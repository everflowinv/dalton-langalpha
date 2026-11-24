"""Three-tier user identity for Dalton permission gates.

Tier is fixed by roster only — there is no UI to change it:

- ``admin``: user_id in ``DALTON_PM_USER_IDS``.
- ``internal``: user email (from the ``users`` table) in ``DALTON_ALLOWED_EMAILS``.
- ``external``: everyone else, which includes every invite-signup member
  (``dalton_members``).

Every lookup failure resolves to ``external`` (fail closed): a raising roster
check can never widen access — an admin-check failure falls through to the
email check rather than granting admin, and a failing email check resolves to
external.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Literal

from src.dalton import settings as dalton_settings
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    COMPANY_SYSTEM_USER_ID,
    is_company_system_user,
)
from src.dalton.settings import (
    get_dalton_allowed_emails,
    is_dalton_local_auth_enabled,
)

logger = logging.getLogger(__name__)

UserTier = Literal["admin", "internal", "external", "company"]
# Strict owner-side classification: "unknown" means the roster lookup failed
# or found no user row, so no isolation rule may be applied to the resource.
OwnerTier = Literal["admin", "internal", "external", "company", "unknown"]

__all__ = [
    "COMPANY_SYSTEM_EMAIL",
    "COMPANY_SYSTEM_USER_ID",
    "OwnerTier",
    "UserTier",
    "classify_owner_tier",
    "classify_owner_tier_sync",
    "classify_owner_tiers_batch",
    "get_primary_admin_user_id",
    "get_primary_admin_user_id_sync",
    "get_user_tier",
    "is_admin_user",
    "is_admin_user_sync",
    "is_internal_or_admin",
    "is_internal_tier",
    "is_team_member_user",
    "list_admin_user_ids",
    "list_admin_user_ids_sync",
]


def is_invite_pm(user_id: str | None) -> bool:
    """Delegate to ``src.dalton.invites.is_invite_pm`` without top-level circular imports."""
    from src.dalton.invites import is_invite_pm as _invites_is_invite_pm

    return _invites_is_invite_pm(user_id)


def _fetch_local_role_sync(user_id: str) -> Literal["admin", "internal", "external"] | None:
    """Read ``dalton_user_roles.tier`` synchronously for non-async callers in ``local`` mode.

    Returns ``None`` on missing row or any DB failure (fail closed).
    """
    uid = str(user_id or "").strip()
    if not uid:
        return None
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
        if not row or not row[0]:
            return None
        tier = str(row[0]).strip().lower()
        if tier in ("admin", "internal", "external"):
            return tier  # type: ignore[return-value]
        return None
    except Exception as exc:
        logger.warning(
            "_fetch_local_role_sync failed for user_id=%s (fail-closed None): %s",
            uid,
            type(exc).__name__,
        )
        return None


def _list_local_admin_ids_sync() -> list[str]:
    """Return ordered admin user IDs from ``dalton_user_roles`` synchronously (fail-closed ``[]``)."""
    try:
        import psycopg

        from src.server.database.pool import get_db_connection_string

        with psycopg.connect(get_db_connection_string(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM dalton_user_roles "
                    "WHERE tier = 'admin' ORDER BY created_at ASC, user_id ASC"
                )
                rows = cur.fetchall()
        return [str(r[0]).strip() for r in rows if r and r[0] and str(r[0]).strip()]
    except Exception as exc:
        logger.warning(
            "_list_local_admin_ids_sync failed (fail-closed []): %s",
            type(exc).__name__,
        )
        return []


def _fetch_local_primary_admin_sync() -> str | None:
    """Return the team credential holder or first admin from PostgreSQL synchronously."""
    try:
        import psycopg

        from src.server.database.pool import get_db_connection_string

        with psycopg.connect(get_db_connection_string(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT h.holder_user_id "
                    "FROM dalton_credential_holder h "
                    "JOIN dalton_user_roles r ON r.user_id = h.holder_user_id "
                    "WHERE h.singleton_id = 1 AND r.tier = 'admin'"
                )
                holder = cur.fetchone()
                if holder and holder[0] and str(holder[0]).strip():
                    return str(holder[0]).strip()
                cur.execute(
                    "SELECT user_id FROM dalton_user_roles "
                    "WHERE tier = 'admin' ORDER BY created_at ASC, user_id ASC LIMIT 1"
                )
                row = cur.fetchone()
                if row and row[0] and str(row[0]).strip():
                    return str(row[0]).strip()
        return None
    except Exception as exc:
        logger.warning(
            "_fetch_local_primary_admin_sync failed (fail-closed None): %s",
            type(exc).__name__,
        )
        return None


def is_internal_tier(user_tier: str | None) -> bool:
    """Return True when ``user_tier`` is an internal member tier (``admin`` or ``internal``)."""
    return (user_tier or "").strip().lower() in ("admin", "internal")


async def is_admin_user(
    user_id: str | None,
    *,
    include_config_defaults: bool = False,
    conn: Any = None,
) -> bool:
    """Return True when ``user_id`` is an administrator.

    - ``local`` auth mode: reads ``dalton_user_roles`` (``tier == 'admin'``); never uses
      ``DALTON_PM_USER_IDS`` or YAML defaults.
    - Everflow / Supabase / OSS mode: checks ``get_dalton_config().pm_user_ids`` when
      ``include_config_defaults=True``, else ``DALTON_PM_USER_IDS`` (``is_invite_pm``).
    """
    uid = str(user_id or "").strip()
    if not uid or is_company_system_user(uid):
        return False
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import get_user_role

            return await get_user_role(uid, conn=conn) == "admin"
        except Exception as exc:
            logger.warning(
                "is_admin_user local role lookup failed for user_id=%s (treated as False): %s",
                uid,
                type(exc).__name__,
            )
            return False
    try:
        if include_config_defaults:
            return uid in set(dalton_settings.get_dalton_settings().pm_user_ids)
        return is_invite_pm(uid)
    except Exception as exc:
        logger.warning(
            "is_admin_user check failed for user_id=%s (treated as False): %s",
            uid,
            type(exc).__name__,
        )
        return False


def is_admin_user_sync(
    user_id: str | None,
    *,
    include_config_defaults: bool = False,
) -> bool:
    """Synchronous counterpart of :func:`is_admin_user` for non-async callers."""
    uid = str(user_id or "").strip()
    if not uid or is_company_system_user(uid):
        return False
    if is_dalton_local_auth_enabled():
        return _fetch_local_role_sync(uid) == "admin"
    try:
        if include_config_defaults:
            return uid in set(dalton_settings.get_dalton_settings().pm_user_ids)
        return uid in set(dalton_settings.get_configured_pm_user_ids())
    except Exception as exc:
        logger.warning(
            "is_admin_user_sync check failed for user_id=%s (treated as False): %s",
            uid,
            type(exc).__name__,
        )
        return False


async def list_admin_user_ids(
    *,
    include_config_defaults: bool = False,
    conn: Any = None,
) -> list[str]:
    """Return ordered administrator ``user_id`` values for the active auth mode.

    - ``local`` mode: queries ``dalton_user_roles`` where ``tier = 'admin'`` (putting the
      singleton ``dalton_credential_holder`` first if it is an active admin).
    - Everflow / Supabase / OSS mode: returns ``DALTON_PM_USER_IDS`` (or config defaults
      when ``include_config_defaults=True``).
    """
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import (
                get_credential_holder_id,
                list_admin_user_ids as _list_local_admins,
            )

            admins = await _list_local_admins(conn=conn)
            holder = await get_credential_holder_id(conn=conn)
            if holder and holder in admins and admins[0] != holder:
                admins = [holder, *[u for u in admins if u != holder]]
            return admins
        except Exception as exc:
            logger.warning(
                "list_admin_user_ids local lookup failed (treated as []): %s",
                type(exc).__name__,
            )
            return []
    if include_config_defaults:
        return [
            str(u).strip()
            for u in dalton_settings.get_dalton_settings().pm_user_ids
            if str(u).strip()
        ]
    return list(dalton_settings.get_configured_pm_user_ids())


def list_admin_user_ids_sync(*, include_config_defaults: bool = False) -> list[str]:
    """Synchronous counterpart of :func:`list_admin_user_ids`."""
    if is_dalton_local_auth_enabled():
        admins = _list_local_admin_ids_sync()
        holder = _fetch_local_primary_admin_sync()
        if holder and holder in admins and admins[0] != holder:
            admins = [holder, *[u for u in admins if u != holder]]
        return admins
    if include_config_defaults:
        return [
            str(u).strip()
            for u in dalton_settings.get_dalton_settings().pm_user_ids
            if str(u).strip()
        ]
    return list(dalton_settings.get_configured_pm_user_ids())


async def get_primary_admin_user_id(*, conn: Any = None) -> str | None:
    """Return the primary administrator / team holder ``user_id`` for the active auth mode.

    - ``local`` mode: returns ``dalton_credential_holder.holder_user_id`` if present,
      otherwise the earliest ``admin`` in ``dalton_user_roles``.
    - Everflow / Supabase / OSS mode: returns ``DALTON_PM_USER_IDS[0]`` or ``None``.
    """
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import (
                get_credential_holder_id,
                get_user_role,
                list_admin_user_ids as _list_local_admins,
            )

            holder = await get_credential_holder_id(conn=conn)
            if holder and await get_user_role(holder, conn=conn) == "admin":
                return holder
            admins = await _list_local_admins(conn=conn)
            return admins[0] if admins else None
        except Exception as exc:
            logger.warning(
                "get_primary_admin_user_id local lookup failed (treated as None): %s",
                type(exc).__name__,
            )
            return None
    pm_ids = dalton_settings.get_configured_pm_user_ids()
    return pm_ids[0] if pm_ids else None


def get_primary_admin_user_id_sync() -> str | None:
    """Synchronous counterpart of :func:`get_primary_admin_user_id`."""
    if is_dalton_local_auth_enabled():
        return _fetch_local_primary_admin_sync()
    pm_ids = dalton_settings.get_configured_pm_user_ids()
    return pm_ids[0] if pm_ids else None


async def is_team_member_user(
    user_id: str | None,
    *,
    email: str | None = None,
    conn: Any = None,
) -> bool:
    """Return True when the user is an active member of this Dalton deployment.

    - ``local`` mode: verifies ``user_id`` has a role row in ``dalton_user_roles``.
    - Everflow / Supabase mode: checks ``DALTON_ALLOWED_EMAILS ∪ dalton_members`` via
      :func:`src.dalton.invites.is_team_member`.
    """
    uid = str(user_id or "").strip()
    if is_dalton_local_auth_enabled():
        if not uid or is_company_system_user(uid):
            return False
        try:
            from src.dalton.auth.roles import get_user_role

            return await get_user_role(uid, conn=conn) in ("admin", "internal", "external")
        except Exception as exc:
            logger.warning(
                "is_team_member_user local lookup failed for user_id=%s (treated as False): %s",
                uid,
                type(exc).__name__,
            )
            return False

    from src.dalton.invites import is_team_member

    resolved_email = str(email or "").strip().lower()
    if not resolved_email and uid:
        try:
            from src.server.database.user import get_user

            user = await get_user(uid, conn=conn) if conn is not None else await get_user(uid)
            resolved_email = str((user or {}).get("email") or "").strip().lower()
        except Exception:
            resolved_email = ""
    if not resolved_email:
        return False
    return (
        await is_team_member(resolved_email, conn=conn)
        if conn is not None
        else await is_team_member(resolved_email)
    )


def classify_owner_tier_sync(owner_id: str | None, owner_email: Any = None) -> OwnerTier:
    """Synchronous strict owner-tier classification for list loops (fail closed to ``unknown``)."""
    uid = str(owner_id or "").strip()
    if not uid:
        return "unknown"
    if is_company_system_user(uid):
        return "company"
    if is_dalton_local_auth_enabled():
        role = _fetch_local_role_sync(uid)
        return role if role in ("admin", "internal", "external") else "unknown"
    try:
        if is_invite_pm(uid):
            return "admin"
        email = str(owner_email or "").strip().lower()
        if not email:
            return "unknown"
        if email in get_dalton_allowed_emails():
            return "internal"
        return "external"
    except Exception:
        return "unknown"


async def get_user_tier(user_id: str | None, *, conn: Any = None) -> UserTier:
    """Resolve a user's access tier; never raises, never returns a wider tier on failure."""
    uid = str(user_id or "").strip()
    if not uid:
        return "external"
    if is_company_system_user(uid):
        return "company"
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import get_user_role

            role = await get_user_role(uid, conn=conn) if conn is not None else await get_user_role(uid)
            if role in ("admin", "internal", "external"):
                return role
        except Exception as exc:
            logger.warning(
                "get_user_tier local role lookup failed for user_id=%s (treated as external): %s",
                uid,
                type(exc).__name__,
            )
        return "external"
    try:
        if is_invite_pm(uid):
            return "admin"
    except Exception as exc:
        # A broken PM check must not hand out admin: fall through to the
        # email check, which itself fails closed to external.
        logger.warning(
            "get_user_tier admin check failed for user_id=%s (continuing as non-admin): %s",
            uid,
            type(exc).__name__,
        )
    try:
        from src.server.database.user import get_user

        user = await get_user(uid, conn=conn) if conn is not None else await get_user(uid)
        email = str((user or {}).get("email") or "").strip().lower()
        if email and email in get_dalton_allowed_emails():
            return "internal"
    except Exception as exc:
        logger.warning(
            "get_user_tier lookup failed for user_id=%s (treated as external): %s",
            uid,
            type(exc).__name__,
        )
    return "external"


async def is_internal_or_admin(user_id: str | None, *, conn: Any = None) -> bool:
    """Return True when ``user_id`` resolves to ``admin`` or ``internal`` tier.

    Fails closed to ``external`` (``False``) on missing user_id or any error
    from ``get_user_tier``.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        tier = await get_user_tier(uid, conn=conn) if conn is not None else await get_user_tier(uid)
    except Exception as exc:
        logger.warning(
            "is_internal_or_admin tier check failed for user_id=%s (treated as external): %s",
            uid,
            type(exc).__name__,
        )
        return False
    return tier in ("admin", "internal")


async def classify_owner_tier(owner_id: str | None, *, conn: Any = None) -> OwnerTier:
    """Strictly classify a *resource owner* for isolation decisions.

    ``get_user_tier`` fails closed to ``external`` — the safe answer for a
    *caller*. For an owner the same fallback would widen access: an internal
    owner misread as external makes their hidden workspace readable (ro) by
    admins, and a PM owner (an invite member outside the email allowlist)
    misread as external loses their admin rights. So here every unresolvable
    step (raising roster check, missing user row, or empty email) returns
    ``unknown``, and callers must hide the resource instead of applying
    either isolation rule.
    """
    uid = str(owner_id or "").strip()
    if not uid:
        return "unknown"
    if is_company_system_user(uid):
        return "company"
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import get_user_role

            role = await get_user_role(uid, conn=conn) if conn is not None else await get_user_role(uid)
            if role in ("admin", "internal", "external"):
                return role
        except Exception as exc:
            logger.warning(
                "classify_owner_tier local role lookup failed for user_id=%s (treated as unknown): %s",
                uid,
                type(exc).__name__,
            )
        return "unknown"
    try:
        if is_invite_pm(uid):
            return "admin"
    except Exception as exc:
        logger.warning(
            "classify_owner_tier admin check failed for user_id=%s (treated as unknown): %s",
            uid,
            type(exc).__name__,
        )
        return "unknown"
    try:
        from src.server.database.user import get_user

        user = await get_user(uid, conn=conn) if conn is not None else await get_user(uid)
        email = str((user or {}).get("email") or "").strip().lower()
        if not email:
            return "unknown"
        if email in get_dalton_allowed_emails():
            return "internal"
        return "external"
    except Exception as exc:
        logger.warning(
            "classify_owner_tier lookup failed for user_id=%s (treated as unknown): %s",
            uid,
            type(exc).__name__,
        )
        return "unknown"


async def classify_owner_tiers_batch(
    owner_ids: Iterable[str | None],
    *,
    conn: Any = None,
) -> dict[str, OwnerTier]:
    """Batch-classify resource owners asynchronously without per-row sync DB queries.

    In ``local`` auth mode, fetches all requested users from ``dalton_user_roles`` in a
    single async query (reusing ``conn`` when provided). Missing rows or DB failures
    resolve fail-closed to ``'unknown'``.
    """
    result: dict[str, OwnerTier] = {}
    to_query: list[str] = []
    for raw in owner_ids:
        uid = str(raw or "").strip()
        if not uid or uid in result:
            continue
        if is_company_system_user(uid):
            result[uid] = "company"
        else:
            to_query.append(uid)
    if not to_query:
        return result
    if is_dalton_local_auth_enabled():
        try:
            from src.dalton.auth.roles import get_user_roles_batch

            roles = (
                await get_user_roles_batch(to_query, conn=conn)
                if conn is not None
                else await get_user_roles_batch(to_query)
            )
        except Exception as exc:
            logger.warning(
                "classify_owner_tiers_batch local role lookup failed (treated as unknown): %s",
                type(exc).__name__,
            )
            roles = {}
        for uid in to_query:
            role = roles.get(uid)
            result[uid] = role if role in ("admin", "internal", "external") else "unknown"
        return result
    for uid in to_query:
        result[uid] = (
            await classify_owner_tier(uid, conn=conn)
            if conn is not None
            else await classify_owner_tier(uid)
        )
    return result
