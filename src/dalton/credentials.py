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
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, NoReturn

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
TEAM_CREDENTIAL_BASE_URL_FORBIDDEN_DETAIL = "团队共用密钥的地址只能由凭据持有人修改"
TEAM_CATALOG_ADMIN_FORBIDDEN_DETAIL = "团队模型目录只能由管理员修改"
TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL = (
    "Database unavailable while verifying team credential permissions"
)
TEAM_SHARED_KEY_ENDPOINT_REFUSED_TYPE = "base_url_blocked"
_TEAM_SETTINGS_LINK = {"url": "/settings?tab=model", "label": "Open Settings"}
_ROUTING_HEADER_CONTAINER_KEYS = (
    "default_headers",
    "additional_headers",
    "extra_headers",
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
    require_holder: bool = False,
) -> str:
    """Pin team credential holder AND authorize ``actor_user_id`` inside the write transaction.

    - Outside ``local`` mode (including ``supabase`` and ``oss``): preserves existing
      ``credential_user_id(actor_user_id)`` without issuing extra SQL statements, and
      enforces ``require_holder`` when requested.
    - In ``local`` mode when team sharing is configured: locks ``dalton_credential_holder``
      and ``dalton_user_roles`` with ``FOR SHARE`` inside ``conn``, raises ``403`` if the
      actor is not an ``admin`` (or not the holder when ``require_holder=True``), raises
      ``503`` on any DB failure, and returns the pinned ``holder_user_id`` for the write
      statement to target.
    """
    if not actor_user_id:
        return actor_user_id
    if not is_dalton_local_auth_enabled():
        holder_id = credential_user_id(actor_user_id)
        if require_holder and holder_id != actor_user_id:
            raise HTTPException(
                status_code=403,
                detail=TEAM_CREDENTIAL_BASE_URL_FORBIDDEN_DETAIL,
            )
        return holder_id

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
    if require_holder and holder_id and uid != holder_id:
        raise HTTPException(
            status_code=403,
            detail=TEAM_CREDENTIAL_BASE_URL_FORBIDDEN_DETAIL,
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


def require_credential_holder(user_id: str) -> None:
    """Raise HTTP 403 if team credential sharing is active and `user_id` is not the holder."""
    holder_id = get_team_credential_holder_id()
    if holder_id and str(user_id or "").strip() != holder_id:
        raise HTTPException(
            status_code=403,
            detail=TEAM_CREDENTIAL_BASE_URL_FORBIDDEN_DETAIL,
        )


def is_catalog_admin(user_id: str | None) -> bool:
    """Unified 3-auth-mode catalog admin predicate (§3.1 #1).

    - ``local`` auth mode: live PG check on ``dalton_user_roles.tier == 'admin'``
      (raises HTTP 503 on DB error, fail-closed).
    - ``supabase`` auth mode: ``user_id`` in ``DALTON_PM_USER_IDS``.
    - ``oss`` loopback trial mode: authenticated non-empty user that is not
      ``COMPANY_SYSTEM_USER_ID`` is the local owner and treated as ``admin``.
    """
    from src.dalton.company_identity import is_company_system_user
    from src.dalton.settings import get_dalton_auth_mode

    uid = str(user_id or "").strip()
    if not uid or is_company_system_user(uid):
        return False
    mode = get_dalton_auth_mode()
    if mode == "local" or is_dalton_local_auth_enabled():
        return _fetch_local_is_admin_sync(uid)
    if mode == "supabase" or is_dalton_supabase_auth_enabled():
        return uid in set(get_configured_pm_user_ids())
    # DALTON_AUTH_MODE=oss (local loopback owner trial mode)
    return mode == "oss"


async def is_catalog_admin_async(user_id: str | None, *, conn: Any = None) -> bool:
    """Async counterpart of :func:`is_catalog_admin` (fail-closed 503 on DB error in local mode)."""
    from src.dalton.company_identity import is_company_system_user
    from src.dalton.settings import get_dalton_auth_mode

    uid = str(user_id or "").strip()
    if not uid or is_company_system_user(uid):
        return False
    mode = get_dalton_auth_mode()
    if mode == "local" or is_dalton_local_auth_enabled():
        from src.dalton.auth.roles import get_user_role

        try:
            return await get_user_role(uid, conn=conn) == "admin"
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Async catalog admin role lookup failed (fail-closed 503): %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail=TEAM_CREDENTIAL_DB_UNAVAILABLE_DETAIL
            ) from exc
    if mode == "supabase" or is_dalton_supabase_auth_enabled():
        return uid in set(get_configured_pm_user_ids())
    return mode == "oss"


def require_catalog_admin(user_id: str | None) -> None:
    """Raise HTTP 403 when ``user_id`` is not authorized to administer the team model catalog."""
    if not is_catalog_admin(user_id):
        raise HTTPException(
            status_code=403,
            detail=TEAM_CATALOG_ADMIN_FORBIDDEN_DETAIL,
        )


async def require_catalog_admin_async(user_id: str | None, *, conn: Any = None) -> None:
    """Async guard raising HTTP 403 when ``user_id`` cannot administer the team model catalog."""
    if not await is_catalog_admin_async(user_id, conn=conn):
        raise HTTPException(
            status_code=403,
            detail=TEAM_CATALOG_ADMIN_FORBIDDEN_DETAIL,
        )


def is_base_url_blocked_exception(exc: BaseException) -> bool:
    """Return True when ``exc`` is a 400 HTTPException with ``detail['type'] == 'base_url_blocked'``."""
    return (
        isinstance(exc, HTTPException)
        and exc.status_code == 400
        and isinstance(exc.detail, dict)
        and exc.detail.get("type") == TEAM_SHARED_KEY_ENDPOINT_REFUSED_TYPE
    )


def uses_shared_team_credential(user_id: str | None) -> bool:
    """Return True when ``user_id`` resolves to another user's team-shared credential.

    Returns False when team sharing is off, ``user_id`` is empty/None, or
    ``user_id`` is the credential holder itself.
    """
    if not user_id:
        return False
    return credential_user_id(user_id) != user_id


def find_disallowed_routing_param_keys(params: Any) -> list[str]:
    """Return sorted endpoint/connection/vertex parameter keys present in ``params``.

    Covers ``ENDPOINT_PARAM_KEYS``, ``CONNECTION_PARAM_KEYS``, and
    ``VERTEX_PARAM_KEYS`` at both top-level and inside ``model_kwargs``.
    """
    from src.server.services.llm.egress import routing_params

    return sorted(routing_params(params))


def is_disallowed_routing_header(name: str) -> bool:
    """Return True for HTTP header names that can alter request routing."""
    low = str(name or "").strip().lower()
    if not low:
        return False
    return low in {"host", ":authority"} or "host" in low or "forward" in low


def find_disallowed_routing_header_keys(params: Any) -> list[str]:
    """Return sorted routing-altering header paths under ``default_headers``/``additional_headers``/``extra_headers``."""
    from src.llms.llm import param_levels

    bad: set[str] = set()
    for prefix, level in param_levels(params):
        for container_key in _ROUTING_HEADER_CONTAINER_KEYS:
            headers = level.get(container_key)
            if isinstance(headers, Mapping):
                for hdr_name in headers:
                    if is_disallowed_routing_header(str(hdr_name)):
                        bad.add(f"{prefix}{container_key}.{hdr_name}")
            elif isinstance(headers, (list, tuple)):
                for item in headers:
                    if isinstance(item, (list, tuple)) and len(item) >= 1:
                        hdr_name = str(item[0])
                        if is_disallowed_routing_header(hdr_name):
                            bad.add(f"{prefix}{container_key}.{hdr_name}")
    return sorted(bad)


def find_disallowed_custom_provider_keys(provider_entry: Any) -> list[str]:
    """Return sorted endpoint/connection/vertex/routing-header keys on a ``custom_providers`` entry.

    Inspects top-level keys (including ``model_kwargs`` and ``default_headers``)
    and any nested ``parameters`` mapping.
    """
    if not isinstance(provider_entry, Mapping):
        return []
    bad = find_disallowed_routing_param_keys(provider_entry)
    bad.extend(find_disallowed_routing_header_keys(provider_entry))
    nested_params = provider_entry.get("parameters")
    if isinstance(nested_params, Mapping):
        bad.extend(
            f"parameters.{k}" for k in find_disallowed_routing_param_keys(nested_params)
        )
        bad.extend(
            f"parameters.{k}" for k in find_disallowed_routing_header_keys(nested_params)
        )
    return sorted(set(bad))


def _raise_shared_key_endpoint_refused(message: str) -> NoReturn:
    raise HTTPException(
        status_code=400,
        detail={
            "message": message,
            "type": TEAM_SHARED_KEY_ENDPOINT_REFUSED_TYPE,
            "link": _TEAM_SETTINGS_LINK,
        },
    )


def enforce_shared_byok_endpoint_policy(
    *,
    user_id: str,
    model_name: str,
    byok_config: Mapping[str, Any],
    base_url: str | None,
    manifest_base_url: str | None,
    custom_config: Mapping[str, Any] | None = None,
    custom_provider_config: Mapping[str, Any] | None = None,
    holder_custom_provider_config: Mapping[str, Any] | None = None,
    require_holder_custom_provider: bool = False,
    holding_provider: str | None = None,
) -> None:
    """Reject custom endpoint/connection/header overrides when using a team-shared BYOK key.

    No-op when team sharing is disabled or ``user_id`` is the credential holder itself.
    When ``credential_user_id(user_id) != user_id``:
    1. Rejects any ``ENDPOINT_PARAM_KEYS``, ``CONNECTION_PARAM_KEYS``,
       ``VERTEX_PARAM_KEYS``, or routing-altering headers (including under
       ``model_kwargs``) in ``custom_config["parameters"]``.
    2. Rejects any such keys on ``custom_provider_config`` (the member's custom
       provider entry, if any), and rejects when the member's ``parent_provider``
       disagrees with the holder's custom provider (or shadowed built-in provider).
    3. When ``require_holder_custom_provider`` is True (non-manifest provider slug),
       requires ``holder_custom_provider_config`` to exist with a valid
       ``parent_provider``; otherwise refuses with 400 before any client is built.
    4. Ensures ``base_url`` comes strictly from the holder's saved non-empty
       ``byok_config["base_url"]`` or ``manifest_base_url`` (from the holder's
       parent provider or the manifest default).
    """
    if not uses_shared_team_credential(user_id):
        return

    if custom_config is not None:
        bad_keys = sorted(
            {
                *find_disallowed_routing_param_keys(custom_config.get("parameters")),
                *find_disallowed_routing_header_keys(custom_config.get("parameters")),
            }
        )
        if bad_keys:
            keys_str = ", ".join(f"parameters.{k}" for k in bad_keys)
            _raise_shared_key_endpoint_refused(
                f"Model '{model_name}' uses a team-shared API key and cannot set custom "
                f"endpoint, connection, or routing header parameters ({keys_str}). "
                "Remove them in Settings."
            )

    if custom_provider_config is not None:
        bad_cp_keys = find_disallowed_custom_provider_keys(custom_provider_config)
        if bad_cp_keys:
            keys_str = ", ".join(bad_cp_keys)
            _raise_shared_key_endpoint_refused(
                f"Model '{model_name}' uses a team-shared API key and its custom provider "
                f"cannot set endpoint, connection, or routing header parameters ({keys_str}). "
                "Remove them in Settings."
            )

    if require_holder_custom_provider:
        holder_parent = (
            holder_custom_provider_config.get("parent_provider")
            if isinstance(holder_custom_provider_config, Mapping)
            else None
        )
        if not isinstance(holder_parent, str) or not holder_parent.strip():
            _raise_shared_key_endpoint_refused(
                f"Model '{model_name}' uses a team-shared API key for a custom provider "
                "that is not defined by the credential holder."
            )

    if custom_provider_config is not None:
        member_parent = custom_provider_config.get("parent_provider")
        if holder_custom_provider_config is not None:
            expected_parent = holder_custom_provider_config.get("parent_provider")
            if member_parent != expected_parent:
                _raise_shared_key_endpoint_refused(
                    f"Model '{model_name}' uses a team-shared API key and its custom provider "
                    f"parent ('{member_parent}') does not match the credential holder's "
                    f"definition ('{expected_parent}')."
                )
        elif holding_provider and member_parent != holding_provider:
            _raise_shared_key_endpoint_refused(
                f"Model '{model_name}' uses a team-shared API key and cannot shadow "
                f"provider '{holding_provider}' with parent '{member_parent}'."
            )

    raw_holder_base_url = byok_config.get("base_url")
    holder_base_url = (
        raw_holder_base_url.strip()
        if isinstance(raw_holder_base_url, str) and raw_holder_base_url.strip()
        else None
    )
    expected_base_url = (
        holder_base_url if holder_base_url is not None else manifest_base_url
    )
    effective_base_url = (
        base_url.strip()
        if isinstance(base_url, str) and base_url.strip()
        else (None if base_url is None else base_url)
    )
    # Reject any personal custom model whose effective resolved endpoint lands on a
    # catalog-defined provider variant's bound_base_url (even when inherited from holder).
    if custom_config is not None:
        from src.dalton.services.team_model_catalog.transport import (
            check_custom_model_against_catalog_variants,
        )

        check_custom_model_against_catalog_variants(
            resolved_provider=holding_provider or str(custom_config.get("provider") or ""),
            resolved_base_url=effective_base_url,
            model_name=model_name,
        )
    if effective_base_url != expected_base_url:
        _raise_shared_key_endpoint_refused(
            f"Model '{model_name}' uses a team-shared API key and can only connect to "
            "the holder's configured base URL or the provider's default endpoint."
        )
