"""Dalton budget gate hooks: runtime gate builder, admission check, and startup wiring verification (P0.8 §2, §7, §8, P0N.6, G1)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from src.config.env import HOST_MODE, USD_TO_CREDITS_RATE
from src.dalton.budget.port import (
    LOCAL_PORT,
    format_daily_budget_exhausted_message,
)
from src.dalton.budget.service import reconcile_and_summarize_day
from src.dalton.llm import role_policy
from src.dalton.llm.role_policy import DaltonStartupConfigError
from src.dalton.settings import (
    get_dalton_settings,
    is_dalton_budget_gate_enabled,
    is_dalton_enabled,
)
from src.ptc_agent.agent.middleware.credit_gate import CreditGateState, CreditLease
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_WARNED_GATE_DISABLED = False


def local_gate_enabled() -> bool:
    """Return True when running in OSS mode with `DALTON_BUDGET_GATE=1` (R2-6)."""
    global _WARNED_GATE_DISABLED
    enabled = HOST_MODE == "oss" and is_dalton_budget_gate_enabled()
    if not enabled and not _WARNED_GATE_DISABLED and HOST_MODE == "oss" and is_dalton_enabled():
        _WARNED_GATE_DISABLED = True
        logger.warning(
            "[DaltonBudget] 本地预算闸门未启用 (DALTON_BUDGET_GATE != 1); "
            "daily USD cap enforcement is currently inactive."
        )
    return enabled


def build_local_run_gate(
    user_id: Optional[str],
    run_id: str,
    token_callback: Any,
    tool_tracker: Any = None,
    model_name: Optional[str] = None,
    is_byok: bool = False,
) -> Optional[CreditGateState]:
    """Construct a `CreditGateState` bound to `LOCAL_PORT` (P0.8 §2)."""
    if not user_id:
        return None
    return CreditGateState(
        run_ref=run_id,
        kind="run",
        port=LOCAL_PORT,
        lease=CreditLease(
            user_id=user_id,
            run_ref=run_id,
            port=LOCAL_PORT,
            is_byok=is_byok,
        ),
        tracker=token_callback,
        tool_tracker=tool_tracker,
        rate_multiplier=1.0,
    )


async def enforce_local_admission(user_id: str, byok: bool = False) -> None:
    """Fail-closed admission check before starting or resuming a run (P0.8 §7, P0N.6 §3, G1.1).

    Even when the main model uses OAuth/BYOK (`byok=True`), subagents and background tasks
    in the same turn consume cash budget (L24). Therefore, `byok` never skips admission.
    Raises 429 (`dalton_daily_budget`) when `available < admission_floor_usd`,
    or 503 (`service_unavailable`) on DB error.
    """
    if not user_id:
        return

    settings = get_dalton_settings().budget
    floor_usd = Decimal(str(settings.admission_floor_usd or 0.25))
    rate = Decimal(str(USD_TO_CREDITS_RATE))

    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                summary = await reconcile_and_summarize_day(conn, user_id)
    except Exception as exc:
        logger.error("[DaltonBudget] admission check DB failure: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Service temporarily unavailable. Please try again shortly.",
                "type": "service_unavailable",
                "retry_after": 30,
            },
            headers={"Retry-After": "30"},
        ) from exc

    decs = summary["_decimals"]
    cap_usd = decs["cap_usd"]
    spent_usd = decs["spent_usd"]
    available = cap_usd - spent_usd
    configured = bool(summary.get("configured", True))

    if not configured or available <= Decimal("0") or available < floor_usd:
        settled_total = decs["settled_usd"] + decs["unreserved_usage_usd"]
        open_total = decs["open_reserved_usd"] + decs["expired_conservative_usd"]
        msg = format_daily_budget_exhausted_message(
            cap_usd, settled_total, open_total, configured=configured
        )
        used_credits = int(spent_usd * rate)
        limit_credits = int(cap_usd * rate)
        rem_credits = max(0, limit_credits - used_credits)
        retry_after = int(summary.get("retry_after_seconds") or 3600)

        raise HTTPException(
            status_code=429,
            detail={
                "message": msg,
                "type": "dalton_daily_budget",
                "used_credits": used_credits,
                "credit_limit": limit_credits,
                "remaining_credits": rem_credits,
                "outstanding_debt": 0,
                "retry_after": retry_after,
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit_credits),
                "X-RateLimit-Remaining": str(rem_credits),
            },
        )


async def verify_local_wiring() -> None:
    """Startup assertion for Dalton local budget gate and role policy (P0.8 §8, P0H §4 P3, P0J.J3).

    Raises `DaltonStartupConfigError` (`RolePolicyError` or missing Dalton migration)
    when startup must fail fast; other transient errors (e.g. DB temporarily unreachable)
    propagate as non-`DaltonStartupConfigError` so `setup.py` logs a warning.
    """
    role_policy.enforce_role_policy_on_startup()

    try:
        if not is_dalton_budget_gate_enabled():
            logger.warning(
                "[DaltonBudget] 本地预算闸门未启用 (DALTON_BUDGET_GATE != 1); "
                "daily USD cap enforcement is currently inactive."
            )
            return

        if USD_TO_CREDITS_RATE != 1000:
            logger.warning(
                "[DaltonBudget] USD_TO_CREDITS_RATE is %s (expected 1000)",
                USD_TO_CREDITS_RATE,
            )
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT version_num FROM alembic_version_dalton LIMIT 1")
                v_row = await cur.fetchone()
                version_num = (v_row or {}).get("version_num")
                if not version_num:
                    raise DaltonStartupConfigError(
                        "[DaltonBudget] alembic_version_dalton has no version_num; "
                        "run Dalton database migrations before starting with DALTON_BUDGET_GATE=1."
                    )

                await cur.execute("SELECT COUNT(*) AS cnt FROM dalton_budget_config")
                c_row = await cur.fetchone()
                cfg_count = int((c_row or {}).get("cnt") or 0)

                await cur.execute("SELECT COUNT(*) AS cnt FROM dalton_budget_task_caps")
                await cur.fetchone()
                await cur.execute("SELECT COUNT(*) AS cnt FROM dalton_budget_reservations")
                await cur.fetchone()

        default_cap = get_dalton_settings().budget.default_daily_cap_usd
        if cfg_count == 0 and default_cap is None:
            logger.warning(
                "[DaltonBudget] Wiring OK (alembic_version_dalton=%s), but no daily_cap_usd is configured yet (fail-closed until set in Settings).",
                version_num,
            )
        else:
            logger.info(
                "[DaltonBudget] Wiring OK (alembic_version_dalton=%s, configs=%d, default_cap=%s)",
                version_num,
                cfg_count,
                default_cap,
            )
    except DaltonStartupConfigError as exc:
        logger.error("[DaltonBudget] Startup wiring verification failed: %s", exc)
        raise
    except Exception as exc:
        if isinstance(exc, psycopg.errors.UndefinedTable) or getattr(exc, "sqlstate", None) == "42P01":
            logger.error("[DaltonBudget] Startup wiring verification failed: %s", exc)
            raise DaltonStartupConfigError(
                f"[DaltonBudget] Missing Dalton database migration: {exc}"
            ) from exc
        logger.warning("[DaltonBudget] Startup wiring verification failed: %s", exc)
        raise

