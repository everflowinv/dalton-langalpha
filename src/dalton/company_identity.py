"""Company system identity, external auth guard, and idempotent budget seeding."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

COMPANY_SYSTEM_USER_ID = "system:dalton-company"
COMPANY_SYSTEM_EMAIL = "company-system@dalton.internal"
COMPANY_SYSTEM_NAME = "Dalton Company System"

_UPSERT_COMPANY_USER_SQL = """
INSERT INTO users (user_id, email, name, created_at, updated_at)
VALUES (%s, %s, %s, NOW(), NOW())
ON CONFLICT (user_id) DO NOTHING
"""

_SEED_COMPANY_BUDGET_SQL = """
INSERT INTO dalton_budget_config (
    user_id, daily_cap_usd, default_task_cap_usd, timezone, updated_at
)
VALUES (%s, %s, %s, %s, NOW())
ON CONFLICT (user_id) DO NOTHING
"""


@dataclass(frozen=True)
class CompanyBudgetSeed:
    daily_cap_usd: Decimal
    default_task_cap_usd: Decimal | None
    timezone: str


def is_company_system_user(user_id: str | None) -> bool:
    """Return True when ``user_id`` names the non-human company execution identity."""
    if not user_id:
        return False
    cleaned = str(user_id).strip()
    return (
        cleaned == COMPANY_SYSTEM_USER_ID
        or cleaned.lower() == COMPANY_SYSTEM_USER_ID
    )


def reject_company_system_auth(user_id: str | None) -> None:
    """Refuse any external HTTP/WebSocket credential resolving to ``system:dalton-company``.

    Internal Python execution paths pass ``COMPANY_SYSTEM_USER_ID`` directly in
    process rather than authenticating over HTTP or WebSocket tokens.
    """
    if is_company_system_user(user_id):
        raise HTTPException(
            status_code=401,
            detail="Company system identity cannot authenticate externally",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )


def _normalize_timezone(raw_tz: Any) -> str:
    candidate = str(raw_tz or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        ZoneInfo(candidate)
        return candidate
    except Exception:
        logger.warning(
            "Invalid budget timezone %r while seeding company budget; falling back to Asia/Shanghai",
            raw_tz,
        )
        return "Asia/Shanghai"


def resolve_company_budget_seed(
    budget_settings: Any = None,
) -> tuple[CompanyBudgetSeed | None, bool]:
    """Resolve initial company budget values from deployment config without network calls.

    Returns ``(seed, valid_config)``:
    - ``(CompanyBudgetSeed(...), True)`` when ``default_daily_cap_usd`` is configured and valid.
    - ``(None, True)`` when ``default_daily_cap_usd`` is ``None`` so ``resolve_user_budget_config``
      preserves its unconfigured fail-closed ``daily_cap_usd=0, configured=False`` semantics.
    - ``(CompanyBudgetSeed(Decimal('0'), None, 'Asia/Shanghai'), False)`` when config loading or
      numeric validation fails, locking a fail-closed ``$0`` cap rather than risking open access.
    """
    try:
        cfg = (
            budget_settings
            if budget_settings is not None
            else get_dalton_settings().budget
        )
        tz = _normalize_timezone(getattr(cfg, "timezone", "Asia/Shanghai"))
        raw_task_cap = getattr(cfg, "default_task_cap_usd", None)
        task_cap: Decimal | None = None
        if raw_task_cap is not None:
            task_cap = Decimal(str(raw_task_cap))
            if task_cap.is_nan() or task_cap.is_infinite() or task_cap <= Decimal("0"):
                raise ValueError(f"Invalid default_task_cap_usd={raw_task_cap!r}")

        raw_daily = getattr(cfg, "default_daily_cap_usd", None)
        if raw_daily is None:
            return None, True

        daily_cap = Decimal(str(raw_daily))
        if daily_cap.is_nan() or daily_cap.is_infinite() or daily_cap < Decimal("0"):
            raise ValueError(f"Invalid default_daily_cap_usd={raw_daily!r}")

        return (
            CompanyBudgetSeed(
                daily_cap_usd=daily_cap,
                default_task_cap_usd=task_cap,
                timezone=tz,
            ),
            True,
        )
    except (InvalidOperation, ValueError, TypeError, Exception) as exc:
        logger.warning(
            "Failed to read valid default budget config for %s (failing closed to $0): %s",
            COMPANY_SYSTEM_USER_ID,
            exc,
        )
        return (
            CompanyBudgetSeed(
                daily_cap_usd=Decimal("0"),
                default_task_cap_usd=None,
                timezone="Asia/Shanghai",
            ),
            False,
        )


def _exec_sync_sql(sync_conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    if hasattr(sync_conn, "exec_driver_sql"):
        res = sync_conn.exec_driver_sql(sql, params)
        return int(getattr(res, "rowcount", 0) or 0)
    with sync_conn.cursor() as cur:
        cur.execute(sql, params)
        return int(getattr(cur, "rowcount", 0) or 0)


def ensure_company_system_identity_sync(
    sync_conn: Any,
    *,
    budget_settings: Any = None,
) -> dict[str, Any]:
    """Idempotently initialize ``system:dalton-company`` and its budget row on a sync connection."""
    user_inserted = (
        _exec_sync_sql(
            sync_conn,
            _UPSERT_COMPANY_USER_SQL,
            (COMPANY_SYSTEM_USER_ID, COMPANY_SYSTEM_EMAIL, COMPANY_SYSTEM_NAME),
        )
        > 0
    )
    seed, valid_config = resolve_company_budget_seed(budget_settings)
    budget_inserted = False
    if seed is not None:
        budget_inserted = (
            _exec_sync_sql(
                sync_conn,
                _SEED_COMPANY_BUDGET_SQL,
                (
                    COMPANY_SYSTEM_USER_ID,
                    seed.daily_cap_usd,
                    seed.default_task_cap_usd,
                    seed.timezone,
                ),
            )
            > 0
        )
    return {
        "user_id": COMPANY_SYSTEM_USER_ID,
        "user_inserted": user_inserted,
        "budget_inserted": budget_inserted,
        "budget_seed": seed,
        "valid_config": valid_config,
    }


async def ensure_company_system_identity(
    *,
    conn: Any = None,
    budget_settings: Any = None,
) -> dict[str, Any]:
    """Async counterpart to ``ensure_company_system_identity_sync``; never overwrites existing rows."""
    seed, valid_config = resolve_company_budget_seed(budget_settings)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                _UPSERT_COMPANY_USER_SQL,
                (COMPANY_SYSTEM_USER_ID, COMPANY_SYSTEM_EMAIL, COMPANY_SYSTEM_NAME),
            )
            user_inserted = (cur.rowcount or 0) > 0
            budget_inserted = False
            if seed is not None:
                await cur.execute(
                    _SEED_COMPANY_BUDGET_SQL,
                    (
                        COMPANY_SYSTEM_USER_ID,
                        seed.daily_cap_usd,
                        seed.default_task_cap_usd,
                        seed.timezone,
                    ),
                )
                budget_inserted = (cur.rowcount or 0) > 0
    return {
        "user_id": COMPANY_SYSTEM_USER_ID,
        "user_inserted": user_inserted,
        "budget_inserted": budget_inserted,
        "budget_seed": seed,
        "valid_config": valid_config,
    }
