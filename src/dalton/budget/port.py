"""LocalBudgetPort compatible with `PlatformCreditGatePort` and `FakePort` (P0.8 §5, P0N.6, G1)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from psycopg.rows import dict_row

from src.config.env import USD_TO_CREDITS_RATE
from src.dalton.budget.service import (
    _local_day_bounds,
    reconcile_and_summarize_day,
    resolve_user_budget_config,
)
from src.dalton.database.budget import (
    acquire_budget_advisory_lock,
    lookup_run_context,
    release_reservation,
    resolve_effective_task_cap,
)
from src.dalton.settings import get_dalton_settings
from src.ptc_agent.agent.middleware.credit_gate import LeaseVerdict
from src.server.database.pool import get_db_connection
from src.server.database.runs import credit_ledger

logger = logging.getLogger(__name__)


def format_daily_budget_exhausted_message(
    cap_usd: Decimal,
    settled_usd: Decimal,
    open_reserved_usd: Decimal,
    configured: bool = True,
) -> str:
    if not configured and cap_usd <= 0:
        return (
            "未设置日预算（当前按 $0.00 拒绝）。"
            "请前往 设置 → 用量与预算 填写每日美元上限后再发起研究任务。"
        )
    return (
        f"今日预算 ${cap_usd:.2f} 已用完（已结算 ${settled_usd:.2f}，"
        f"未结算预留 ${open_reserved_usd:.2f}）。"
        "订阅模型不占现金额度，但本轮的子智能体和后台任务使用现金模型。"
        "可在 设置 → 用量与预算 调高额度，或等北京时间 00:00 重置。"
    )


def format_task_cap_exhausted_message(task_cap_usd: Decimal, used_usd: Decimal) -> str:
    return (
        f"本任务预算 ${task_cap_usd:.2f} 已用完（已用 ${used_usd:.2f}）。"
        "确认继续请点“继续”，将按新任务重新计额。"
    )


async def acquire_cash_lease_tx(
    conn,
    *,
    user_id: str,
    run_ref: str,
    spent_credits: float = 0.0,
    call_id: Optional[str] = None,
    min_floor_usd: Optional[Decimal | float] = None,
    lease_ttl_seconds: Optional[int] = None,
) -> LeaseVerdict:
    """Shared atomic PostgreSQL transaction entry for cash budget leasing.

    Used by both regular ``LocalBudgetPort.acquire`` and unattended cash fallback:
    1. Resolves user budget config (including users without a ``dalton_budget_config``
       row who inherit ``default_daily_cap_usd``).
    2. Serializes under the single transaction-scoped advisory lock
       ``acquire_budget_advisory_lock(conn, user_id, b_day)`` (inside
       ``reconcile_and_summarize_day`` and explicitly before fallback checks).
    3. Creates/updates ``dalton_budget_reservations`` via ``ON CONFLICT (run_ref)``.
    4. When ``min_floor_usd`` / ``call_id`` is supplied (unattended cash fallback),
       checks ``(run_ref, call_id)`` idempotency and existing headroom before
       extending ``reserved_usd`` so retries never double-deduct.
    """
    settings = get_dalton_settings().budget
    lease_ttl = int(
        lease_ttl_seconds if lease_ttl_seconds is not None else (settings.lease_ttl_seconds or 300)
    )
    rate = Decimal(str(USD_TO_CREDITS_RATE))
    is_fallback_mode = min_floor_usd is not None or call_id is not None

    cfg = await resolve_user_budget_config(conn, user_id)
    b_day, _, _, _ = _local_day_bounds(cfg["timezone"])
    if is_fallback_mode:
        await acquire_budget_advisory_lock(conn, user_id, b_day)
    summary = await reconcile_and_summarize_day(conn, user_id)

    spent_usd_reported = max(
        Decimal("0"),
        (Decimal(str(spent_credits)) / rate).quantize(Decimal("0.000001")),
    )

    # Check existing reservation row for this run_ref
    rows = summary["_raw_rows"]
    existing = next(
        (r for r in rows if str(r["run_ref"]) == str(run_ref)),
        None,
    )

    if existing is None:
        thread_id, workspace_id = await lookup_run_context(conn, run_ref)
        task_cap = await resolve_effective_task_cap(
            conn,
            user_id,
            thread_id,
            workspace_id,
            cfg["default_task_cap_usd"],
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_budget_reservations (
                    user_id, budget_day, run_ref, thread_id, workspace_id,
                    reserved_usd, observed_usd, task_cap_usd, state,
                    generation, expires_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    0, %s, %s, 'open',
                    1, NOW() + (%s || ' seconds')::interval
                )
                ON CONFLICT (run_ref) DO UPDATE
                SET observed_usd = GREATEST(dalton_budget_reservations.observed_usd, EXCLUDED.observed_usd),
                    renewed_at = NOW()
                RETURNING *,
                    (SELECT COALESCE(
                         SUM(dalton_cash_usd(token_usage::jsonb, total_credits, infrastructure_credits, %s)),
                         0
                     )
                     FROM conversation_usages
                     WHERE conversation_response_id = %s) AS usage_usd
                """,
                (
                    user_id,
                    b_day,
                    run_ref,
                    thread_id,
                    workspace_id,
                    spent_usd_reported,
                    task_cap,
                    str(lease_ttl),
                    rate,
                    run_ref,
                ),
            )
            existing = dict(await cur.fetchone() or {})
            existing["charge_usd"] = max(
                Decimal(str(existing.get("reserved_usd") or "0")),
                Decimal(str(existing.get("observed_usd") or "0")),
                Decimal(str(existing.get("usage_usd") or "0")),
            )
    else:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_budget_reservations
                SET observed_usd = GREATEST(observed_usd, %s),
                    renewed_at = NOW()
                WHERE run_ref = %s
                RETURNING *
                """,
                (spent_usd_reported, run_ref),
            )
            updated = await cur.fetchone()
            if updated:
                existing.update(dict(updated))
                existing["charge_usd"] = max(
                    Decimal(str(existing.get("reserved_usd") or "0")),
                    Decimal(str(existing.get("observed_usd") or "0")),
                    Decimal(str(existing.get("usage_usd") or "0")),
                )

    observed_usd = Decimal(str(existing.get("observed_usd") or "0"))
    usage_for_run = Decimal(str(existing.get("usage_usd") or "0"))
    reserved_usd = Decimal(str(existing.get("reserved_usd") or "0"))
    task_cap_usd = (
        Decimal(str(existing["task_cap_usd"]))
        if existing.get("task_cap_usd") is not None
        else None
    )
    generation = int(existing.get("generation") or 1)

    decs = summary["_decimals"]
    daily_cap = decs["cap_usd"]
    spent_today = decs["spent_usd"]
    configured = bool(summary.get("configured", cfg.get("configured", True)))
    # If existing was already in summary["spent_usd"], subtract its previous charge
    was_in_summary = any(str(r["run_ref"]) == str(run_ref) for r in rows)
    prev_charge_in_summary = (
        Decimal(
            str(
                next(
                    r["charge_usd"]
                    for r in rows
                    if str(r["run_ref"]) == str(run_ref)
                )
            )
        )
        if was_in_summary
        else Decimal("0")
    )

    this_floor = max(observed_usd, usage_for_run)
    others = spent_today - prev_charge_in_summary
    available = daily_cap - others - this_floor
    task_left = (task_cap_usd - this_floor) if task_cap_usd is not None else None

    chunk_usd = Decimal(str(settings.reserve_chunk_usd or 1.00))
    req_floor = (
        max(Decimal("0"), Decimal(str(min_floor_usd)))
        if min_floor_usd is not None
        else Decimal("0")
    )

    # Idempotent short-circuit for unattended cash fallback: same (run_ref, call_id)
    # or already-sufficient open reservation returns existing verdict without raising reserved_usd.
    if is_fallback_mode:
        prev_call_id = existing.get("last_fallback_call_id")
        existing_open = str(existing.get("state") or "open") == "open"
        same_call = bool(call_id and prev_call_id and str(prev_call_id) == str(call_id))
        headroom_ok = (
            existing_open
            and configured
            and daily_cap > Decimal("0")
            and reserved_usd >= this_floor + req_floor
            and reserved_usd > Decimal("0")
            and (task_cap_usd is None or reserved_usd <= task_cap_usd)
        )
        if (same_call and reserved_usd > Decimal("0")) or headroom_ok:
            if call_id and not same_call:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_budget_reservations
                        SET last_fallback_call_id = %s,
                            renewed_at = NOW(),
                            expires_at = NOW() + (%s || ' seconds')::interval
                        WHERE run_ref = %s
                        """,
                        (str(call_id), str(lease_ttl), run_ref),
                    )
            return LeaseVerdict(
                granted=True,
                ceiling_credits=float(reserved_usd * rate),
                ttl_seconds=float(lease_ttl),
                generation=generation,
            )

    if task_left is not None:
        grant = min(chunk_usd, available, task_left)
    else:
        grant = min(chunk_usd, available)

    can_grant = (
        configured
        and daily_cap > Decimal("0")
        and grant > Decimal("0")
        and (not is_fallback_mode or grant >= req_floor)
    )
    if can_grant:
        new_reserved = max(reserved_usd, this_floor + grant)
        async with conn.cursor(row_factory=dict_row) as cur:
            if call_id is not None:
                await cur.execute(
                    """
                    UPDATE dalton_budget_reservations
                    SET reserved_usd = %s,
                        state = CASE WHEN state = 'released' THEN 'open' ELSE state END,
                        renewed_at = NOW(),
                        expires_at = NOW() + (%s || ' seconds')::interval,
                        generation = generation + 1,
                        last_fallback_call_id = %s
                    WHERE run_ref = %s
                    RETURNING generation
                    """,
                    (new_reserved, str(lease_ttl), str(call_id), run_ref),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_budget_reservations
                    SET reserved_usd = %s,
                        state = CASE WHEN state = 'released' THEN 'open' ELSE state END,
                        renewed_at = NOW(),
                        expires_at = NOW() + (%s || ' seconds')::interval,
                        generation = generation + 1
                    WHERE run_ref = %s
                    RETURNING generation
                    """,
                    (new_reserved, str(lease_ttl), run_ref),
                )
            g_row = await cur.fetchone()
            if g_row:
                generation = int(g_row["generation"])

        return LeaseVerdict(
            granted=True,
            ceiling_credits=float(new_reserved * rate),
            ttl_seconds=float(lease_ttl),
            generation=generation,
        )

    # Denied: determine whether task cap or daily cap triggered
    task_blocked = (
        task_left is not None
        and (task_left <= Decimal("0") or (is_fallback_mode and task_left < req_floor))
        and available > Decimal("0")
        and (not is_fallback_mode or available >= req_floor)
    )
    if task_blocked:
        q_type = "dalton_task_cap"
        msg = format_task_cap_exhausted_message(
            task_cap_usd or Decimal("0"), this_floor
        )
        cap_val = float(task_cap_usd or Decimal("0"))
    else:
        q_type = "dalton_daily_budget"
        settled_total = decs["settled_usd"] + decs["unreserved_usage_usd"]
        open_total = decs["open_reserved_usd"] + decs["expired_conservative_usd"]
        msg = format_daily_budget_exhausted_message(
            daily_cap,
            settled_total,
            open_total,
            configured=configured,
        )
        cap_val = float(daily_cap)

    return LeaseVerdict(
        granted=False,
        ceiling_credits=float(this_floor * rate),
        ttl_seconds=float(lease_ttl),
        generation=generation,
        quota={
            "type": q_type,
            "message": msg,
            "spent_usd": float(others + this_floor),
            "cap_usd": cap_val,
            "open_reservations_usd": float(decs["open_reserved_usd"]),
        },
    )


async def acquire_cash_lease(
    *,
    user_id: str,
    run_ref: str,
    call_id: Optional[str] = None,
    min_floor_usd: Optional[Decimal | float] = None,
    spent_credits: float = 0.0,
    lease_ttl_seconds: Optional[int] = None,
    conn=None,
) -> LeaseVerdict:
    """Open a transaction (or reuse ``conn``) and execute :func:`acquire_cash_lease_tx`."""
    if conn is not None:
        return await acquire_cash_lease_tx(
            conn,
            user_id=user_id,
            run_ref=run_ref,
            spent_credits=spent_credits,
            call_id=call_id,
            min_floor_usd=min_floor_usd,
            lease_ttl_seconds=lease_ttl_seconds,
        )
    async with get_db_connection() as own_conn:
        async with own_conn.transaction():
            return await acquire_cash_lease_tx(
                own_conn,
                user_id=user_id,
                run_ref=run_ref,
                spent_credits=spent_credits,
                call_id=call_id,
                min_floor_usd=min_floor_usd,
                lease_ttl_seconds=lease_ttl_seconds,
            )


class LocalBudgetPort:
    """Stateless local daily USD budget port backed by Postgres."""

    async def acquire(
        self,
        user_id: str,
        run_ref: str,
        spent_credits: float,
        rate_multiplier: float = 1.0,
        byok: bool = False,
    ) -> Optional[LeaseVerdict]:
        """Acquire or renew a cash budget lease for `run_ref` (G1.1 / P0N.6).

        Even when the main turn uses an OAuth subscription (`byok=True`), subagents
        on cash models and background models (`deepseek-flash`) share the same lease
        and spend cash credits. Therefore, `byok` never bypasses the daily USD cap.
        """
        settings = get_dalton_settings().budget
        lease_ttl = int(settings.lease_ttl_seconds or 300)

        try:
            async with get_db_connection() as conn:
                async with conn.transaction():
                    return await acquire_cash_lease_tx(
                        conn,
                        user_id=user_id,
                        run_ref=run_ref,
                        spent_credits=spent_credits,
                        lease_ttl_seconds=lease_ttl,
                    )
        except Exception as exc:
            logger.error("[DaltonBudget] acquire failed for run_ref=%s: %s", run_ref, exc, exc_info=True)
            return None

    async def release(
        self,
        user_id: str,
        run_ref: str,
        generation: Optional[int] = None,
    ) -> None:
        try:
            async with get_db_connection() as conn:
                async with conn.transaction():
                    await release_reservation(conn, run_ref, generation=generation)
                    # Trigger a lazy reconcile right away if usage was already settled
                    await reconcile_and_summarize_day(conn, user_id)
        except Exception as exc:
            logger.warning("[DaltonBudget] release failed for run_ref=%s: %s", run_ref, exc)

    async def capability(self) -> Optional[dict]:
        settings = get_dalton_settings().budget
        return {
            "enabled": True,
            "lease_ttl_seconds": int(settings.lease_ttl_seconds or 300),
            "mode": "dalton-local",
        }

    async def heartbeat(
        self,
        kind: credit_ledger.RunKind,
        run_ref: str,
        credits: float,
    ) -> Optional[bool]:
        return await credit_ledger.heartbeat(kind, run_ref, credits)


LOCAL_PORT = LocalBudgetPort()
