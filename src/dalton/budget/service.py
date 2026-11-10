"""Dalton local daily USD budget calculation, reconciliation, and read/write APIs (P0.8 §4, §9, §10, P0N.6, G1).

Hard rule: Only shared daily USD cap and optional task cap are supported.
Zero call count, retry count, or sub-pool limits.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from src.config.env import USD_TO_CREDITS_RATE
from src.dalton.database.budget import (
    acquire_budget_advisory_lock,
    delete_task_cap,
    fetch_budget_config,
    fetch_day_reservations,
    fetch_journal_cash_today,
    fetch_today_token_usages,
    fetch_unreserved_usage_today,
    list_task_caps,
    reconcile_reservations_sql,
    upsert_budget_config,
    upsert_task_cap,
    verify_task_cap_scope_owner,
)
from src.dalton.database.llm_journal import fetch_journal_rows_in_window
from src.dalton.llm.notional_pricing import (
    build_notional_price_index,
    calculate_notional_cost,
    extract_flat_rates_from_card,
    is_subscription_model,
    resolve_notional_card,
)
from src.dalton.llm.overlay import load_overlay_data
from src.dalton.settings import get_dalton_settings, is_dalton_budget_gate_enabled
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


def _local_day_bounds(tz_name: str, now_utc: datetime | None = None) -> tuple[date, datetime, datetime, int]:
    """Return `(budget_day, day_start_utc, day_end_utc, seconds_until_midnight)` in `tz_name`."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")

    now = now_utc or datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    b_day = local_now.date()
    local_start = datetime.combine(b_day, time.min, tzinfo=tz)
    local_end = datetime.combine(b_day + timedelta(days=1), time.min, tzinfo=tz)
    retry_after = max(1, int((local_end - local_now).total_seconds()))
    return (
        b_day,
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
        retry_after,
    )


def _extract_rates_from_card(np: dict[str, Any]) -> dict[str, float]:
    return extract_flat_rates_from_card(np)


def _notional_price_index() -> dict[str, dict[str, Any]]:
    """Build `{model_key_or_ref: normalized_notional_card}` for subscription/OAuth models."""
    return build_notional_price_index(overlay_loader=load_overlay_data)


def _is_subscription_model(model_name: str, billing_type: str = "") -> bool:
    """Return True when `model_name` or `billing_type` denotes a subscription/OAuth model."""
    return is_subscription_model(model_name, billing_type, overlay_loader=load_overlay_data)


def compute_subscription_notional_usd(token_usage_rows: list[dict[str, Any]]) -> Decimal:
    """Estimate nominal USD consumed today by subscription (`pricing_type=='subscription'`) and OAuth models."""
    idx = _notional_price_index()
    total = Decimal("0")
    for row in token_usage_rows:
        tu = row.get("token_usage")
        if not isinstance(tu, dict):
            continue
        per_call = tu.get("per_call_costs")
        if isinstance(per_call, list) and per_call:
            for c in per_call:
                if not isinstance(c, dict):
                    continue
                m_name = str(c.get("model_name") or "unknown")
                b_type = str(c.get("billing_type") or "platform")
                if not _is_subscription_model(m_name, b_type):
                    continue
                if "notional_cost" in c and c["notional_cost"] is not None:
                    total += Decimal(str(c["notional_cost"]))
                    continue
                cost_dec = Decimal(str(c.get("cost") or "0"))
                if cost_dec > Decimal("0"):
                    total += cost_dec
                    continue
                card = resolve_notional_card(m_name, index=idx)
                if card is not None:
                    usd, _ = calculate_notional_cost(
                        card,
                        input_tokens=int(c.get("input_tokens") or 0),
                        output_tokens=int(c.get("output_tokens") or 0),
                        cached_tokens=int(c.get("cached_tokens") or 0),
                    )
                    total += Decimal(str(usd))
            continue

        by_model = tu.get("by_model")
        if not isinstance(by_model, dict):
            continue
        for model_name, stats in by_model.items():
            if not isinstance(stats, dict):
                continue
            m_name = str(model_name)
            rates_list = stats.get("rates") or []
            has_oauth_rate = any(
                isinstance(r, dict) and r.get("billing_type") == "oauth"
                for r in rates_list
            )
            is_sub = _is_subscription_model(m_name, "oauth" if has_oauth_rate else "")
            if (
                "notional_cost" in stats
                and stats["notional_cost"] is not None
                and (is_sub or Decimal(str(stats["notional_cost"])) > Decimal("0"))
            ):
                total += Decimal(str(stats["notional_cost"]))
                continue
            if has_oauth_rate and stats.get("total_cost"):
                total += Decimal(str(stats["total_cost"]))
                continue

            card = resolve_notional_card(m_name, index=idx)
            if card is not None and (is_sub or float(stats.get("total_cost") or 0.0) == 0.0):
                in_tok = int(stats.get("input_tokens") or 0)
                out_tok = int(stats.get("output_tokens") or 0)
                cached_tok = int(stats.get("cached_tokens") or 0)
                call_count = int(stats.get("call_count") or 1)
                usd, _ = calculate_notional_cost(
                    card,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cached_tokens=cached_tok,
                    legacy_aggregate_call_count=call_count,
                )
                total += Decimal(str(usd))
            elif is_sub and stats.get("total_cost"):
                total += Decimal(str(stats["total_cost"]))
    return total.quantize(Decimal("0.000001"))


def _resolve_provider_for_model(model_name: str, billing_type: str = "platform") -> str:
    if model_name.startswith("grok-") and model_name.endswith("-oauth"):
        return "xai-oauth"
    try:
        from src.llms.pricing_utils import detect_provider_for_model

        prov = detect_provider_for_model(model_name, billing_type=billing_type)
        if prov:
            return str(prov)
    except Exception:
        pass
    if model_name.startswith("ocb-"):
        return "openclaw-broker"
    if model_name.endswith("-oauth"):
        if model_name.startswith("claude-"):
            return "claude-oauth"
        if model_name.startswith("gpt-"):
            return "codex-oauth"
        if model_name.startswith("gemini-"):
            return "antigravity-oauth"
        if model_name.startswith("muse-"):
            return "muse-oauth"
        if model_name.startswith("grok-"):
            return "xai-oauth"
    if model_name.startswith("glm-") and model_name.endswith("-plan"):
        return "z-ai-cn-coding"
    if model_name.startswith("grok-"):
        return "xai"
    if model_name.startswith("deepseek-"):
        return "deepseek"
    return "unknown"


def compute_provider_breakdown(
    token_usage_rows: list[dict[str, Any]],
    journal_rows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate today's `cash_usd`, `notional_usd`, and `calls` by provider (P0N.6)."""
    idx = _notional_price_index()
    buckets: dict[str, dict[str, Any]] = {}

    def _ensure(prov: str) -> dict[str, Any]:
        if prov not in buckets:
            buckets[prov] = {
                "cash_usd": Decimal("0"),
                "notional_usd": Decimal("0"),
                "calls": 0,
            }
        return buckets[prov]

    for row in token_usage_rows:
        tu = row.get("token_usage")
        if not isinstance(tu, dict):
            continue
        per_call = tu.get("per_call_costs")
        if isinstance(per_call, list) and per_call:
            for c in per_call:
                if not isinstance(c, dict):
                    continue
                m_name = str(c.get("model_name") or "unknown")
                b_type = str(c.get("billing_type") or "platform")
                is_sub = _is_subscription_model(m_name, b_type)
                prov = _resolve_provider_for_model(m_name, "oauth" if is_sub else b_type)
                b = _ensure(prov)
                b["calls"] += 1
                cost_dec = Decimal(str(c.get("cost") or "0"))
                if is_sub:
                    if "notional_cost" in c and c["notional_cost"] is not None:
                        b["notional_usd"] += Decimal(str(c["notional_cost"]))
                    elif cost_dec > Decimal("0"):
                        b["notional_usd"] += cost_dec
                    else:
                        card = resolve_notional_card(m_name, index=idx)
                        if card is not None:
                            in_tok = int(c.get("input_tokens") or 0)
                            out_tok = int(c.get("output_tokens") or 0)
                            cached_tok = int(c.get("cached_tokens") or 0)
                            est, _ = calculate_notional_cost(
                                card,
                                input_tokens=in_tok,
                                output_tokens=out_tok,
                                cached_tokens=cached_tok,
                            )
                            b["notional_usd"] += Decimal(str(est))
                else:
                    b["cash_usd"] += cost_dec

    for j in journal_rows or []:
        if not isinstance(j, dict) or j.get("state") not in ("done", "indeterminate"):
            continue
        m_name = str(j.get("model_key") or "unknown")
        b_type = str(j.get("billing_type") or "platform")
        is_sub = _is_subscription_model(m_name, b_type)
        prov = str(j.get("provider") or _resolve_provider_for_model(m_name, "oauth" if is_sub else b_type))
        b = _ensure(prov)
        b["calls"] += 1
        cash_val = Decimal(str(j.get("cash_usd") or "0"))
        notional_val = Decimal(str(j.get("notional_usd") or "0"))
        if is_sub:
            b["notional_usd"] += notional_val if notional_val > Decimal("0") else cash_val
        else:
            b["cash_usd"] += cash_val
            b["notional_usd"] += notional_val

    return {
        prov: {
            "cash_usd": float(vals["cash_usd"].quantize(Decimal("0.000001"))),
            "notional_usd": float(vals["notional_usd"].quantize(Decimal("0.000001"))),
            "calls": int(vals["calls"]),
        }
        for prov, vals in sorted(buckets.items())
    }


async def resolve_user_budget_config(conn: Any, user_id: str) -> dict[str, Any]:
    """Read user budget config from DB, falling back to `dalton.yaml`.

    If no DB row exists and `default_daily_cap_usd` is null, `daily_cap_usd` is 0 (fail-closed)
    and `configured` is False.
    """
    row = await fetch_budget_config(conn, user_id)
    settings = get_dalton_settings().budget
    if row is not None:
        return {
            "user_id": user_id,
            "daily_cap_usd": Decimal(str(row["daily_cap_usd"])),
            "default_task_cap_usd": (
                Decimal(str(row["default_task_cap_usd"]))
                if row.get("default_task_cap_usd") is not None
                else (
                    Decimal(str(settings.default_task_cap_usd))
                    if settings.default_task_cap_usd is not None
                    else None
                )
            ),
            "timezone": str(row.get("timezone") or settings.timezone or "Asia/Shanghai"),
            "configured": True,
        }

    if settings.default_daily_cap_usd is not None:
        return {
            "user_id": user_id,
            "daily_cap_usd": Decimal(str(settings.default_daily_cap_usd)),
            "default_task_cap_usd": (
                Decimal(str(settings.default_task_cap_usd))
                if settings.default_task_cap_usd is not None
                else None
            ),
            "timezone": str(settings.timezone or "Asia/Shanghai"),
            "configured": True,
        }

    return {
        "user_id": user_id,
        "daily_cap_usd": Decimal("0"),
        "default_task_cap_usd": (
            Decimal(str(settings.default_task_cap_usd))
            if settings.default_task_cap_usd is not None
            else None
        ),
        "timezone": str(settings.timezone or "Asia/Shanghai"),
        "configured": False,
    }


async def reconcile_and_summarize_day(
    conn: Any,
    user_id: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Run lazy reconciliation inside caller's transaction and return full today summary."""
    cfg = await resolve_user_budget_config(conn, user_id)
    b_day, day_start_utc, day_end_utc, retry_after = _local_day_bounds(
        cfg["timezone"], now_utc=now_utc
    )
    await acquire_budget_advisory_lock(conn, user_id, b_day)

    rate = Decimal(str(USD_TO_CREDITS_RATE))
    await reconcile_reservations_sql(conn, user_id, rate)

    rows = await fetch_day_reservations(conn, user_id, b_day, rate)
    unreserved_usage_usd = await fetch_unreserved_usage_today(
        conn, user_id, day_start_utc, day_end_utc, rate
    )
    journal_cash_usd = await fetch_journal_cash_today(
        conn, day_start_utc, day_end_utc, user_id=user_id
    )
    unreserved_usd = unreserved_usage_usd + journal_cash_usd

    tu_rows = await fetch_today_token_usages(conn, user_id, day_start_utc, day_end_utc)
    journal_rows = await fetch_journal_rows_in_window(
        day_start_utc, day_end_utc, user_id=user_id, conn=conn
    )
    journal_notional_usd = sum(
        (
            Decimal(str(j.get("notional_usd") or "0"))
            for j in journal_rows
            if isinstance(j, dict) and j.get("state") in ("done", "indeterminate")
        ),
        Decimal("0"),
    )
    sub_notional_usd = compute_subscription_notional_usd(tu_rows) + journal_notional_usd
    by_provider = compute_provider_breakdown(tu_rows, journal_rows)

    settled_usd = Decimal("0")
    open_reserved_usd = Decimal("0")
    expired_conservative_usd = Decimal("0")
    runs_out: list[dict[str, Any]] = []

    for r in rows:
        state = str(r["state"])
        charge = Decimal(str(r["charge_usd"] or "0"))
        if state == "settled":
            settled_usd += charge
        elif state == "expired":
            expired_conservative_usd += charge
        else:
            open_reserved_usd += charge

        if len(runs_out) < 20:
            created = r.get("created_at")
            runs_out.append(
                {
                    "run_ref": str(r["run_ref"]),
                    "thread_id": str(r["thread_id"]) if r.get("thread_id") else None,
                    "workspace_id": str(r["workspace_id"]) if r.get("workspace_id") else None,
                    "state": state,
                    "reserved_usd": float(Decimal(str(r["reserved_usd"]))),
                    "observed_usd": float(Decimal(str(r["observed_usd"]))),
                    "charge_usd": float(charge),
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
                }
            )

    spent_usd = settled_usd + open_reserved_usd + expired_conservative_usd + unreserved_usd
    cap_usd = cfg["daily_cap_usd"]
    remaining_usd = max(Decimal("0"), cap_usd - spent_usd)

    return {
        "day": b_day.isoformat(),
        "timezone": cfg["timezone"],
        "configured": cfg["configured"],
        "budget_gate_enabled": bool(is_dalton_budget_gate_enabled()),
        "cap_usd": float(cap_usd),
        "default_task_cap_usd": (
            float(cfg["default_task_cap_usd"])
            if cfg["default_task_cap_usd"] is not None
            else None
        ),
        "settled_usd": float(settled_usd),
        "open_reserved_usd": float(open_reserved_usd),
        "expired_conservative_usd": float(expired_conservative_usd),
        "unreserved_usage_usd": float(unreserved_usd),
        "journal_cash_usd": float(journal_cash_usd),
        "spent_usd": float(spent_usd),
        "remaining_usd": float(remaining_usd),
        "subscription_notional_usd": float(sub_notional_usd),
        "by_provider": by_provider,
        "retry_after_seconds": retry_after,
        "runs": runs_out,
        "_raw_rows": rows,
        "_decimals": {
            "cap_usd": cap_usd,
            "settled_usd": settled_usd,
            "open_reserved_usd": open_reserved_usd,
            "expired_conservative_usd": expired_conservative_usd,
            "unreserved_usage_usd": unreserved_usd,
            "journal_cash_usd": journal_cash_usd,
            "spent_usd": spent_usd,
            "remaining_usd": remaining_usd,
        },
    }


async def get_budget_today(*, user_id: str) -> dict[str, Any]:
    """Public read function (P0.8 §10): reconcile and return today's budget state.

    Used by `/api/v1/dalton/budget/today`, PK preflight, Research Director, and PS automations.
    """
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
            summary.pop("_raw_rows", None)
            summary.pop("_decimals", None)
            return summary


async def get_run_charge_usd(run_ref: str) -> Decimal | None:
    """Public read function (P0.8 §10): return current `charge(r)` in USD for `run_ref`, or None."""
    rate = Decimal(str(USD_TO_CREDITS_RATE))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    r.state,
                    r.reserved_usd,
                    r.observed_usd,
                    r.settled_usd,
                    COALESCE(
                        SUM(dalton_cash_usd(cu.token_usage::jsonb, cu.total_credits, cu.infrastructure_credits, %s)),
                        0
                    ) AS usage_usd
                FROM dalton_budget_reservations r
                LEFT JOIN conversation_usages cu
                    ON cu.conversation_response_id = r.run_ref
                WHERE r.run_ref = %s
                GROUP BY r.reservation_id
                """,
                (rate, run_ref),
            )
            row = await cur.fetchone()
            if not row:
                return None
            state = str(row["state"])
            usage_usd = Decimal(str(row["usage_usd"] or "0"))
            reserved_usd = Decimal(str(row["reserved_usd"] or "0"))
            observed_usd = Decimal(str(row["observed_usd"] or "0"))
            settled_usd = (
                Decimal(str(row["settled_usd"]))
                if row.get("settled_usd") is not None
                else None
            )
            if state == "settled":
                return settled_usd if settled_usd is not None else usage_usd
            if state == "expired":
                return (
                    settled_usd
                    if settled_usd is not None
                    else max(reserved_usd, observed_usd, usage_usd)
                )
            return max(reserved_usd, observed_usd, usage_usd)


async def get_budget_settings(user_id: str) -> dict[str, Any]:
    """Return `{daily_cap_usd, default_task_cap_usd, timezone, configured, budget_gate_enabled, task_caps: [...]}`."""
    async with get_db_connection() as conn:
        cfg = await resolve_user_budget_config(conn, user_id)
        tcaps = await list_task_caps(conn, user_id)
        return {
            "daily_cap_usd": float(cfg["daily_cap_usd"]) if cfg["configured"] else None,
            "default_task_cap_usd": (
                float(cfg["default_task_cap_usd"])
                if cfg["default_task_cap_usd"] is not None
                else None
            ),
            "timezone": cfg["timezone"],
            "configured": cfg["configured"],
            "budget_gate_enabled": bool(is_dalton_budget_gate_enabled()),
            "task_caps": [
                {
                    "scope_kind": r["scope_kind"],
                    "scope_id": str(r["scope_id"]),
                    "scope_name": r.get("scope_name") or str(r["scope_id"]),
                    "cap_usd": float(Decimal(str(r["cap_usd"]))),
                    "updated_at": (
                        r["updated_at"].isoformat()
                        if hasattr(r.get("updated_at"), "isoformat")
                        else str(r.get("updated_at"))
                    ),
                }
                for r in tcaps
            ],
        }


async def save_budget_settings(
    user_id: str,
    *,
    daily_cap_usd: float | Decimal,
    default_task_cap_usd: float | Decimal | None = None,
    timezone_str: str = "Asia/Shanghai",
) -> dict[str, Any]:
    # Validate timezone via ZoneInfo
    ZoneInfo(timezone_str)
    cap_dec = Decimal(str(daily_cap_usd))
    if cap_dec < 0:
        raise ValueError("daily_cap_usd must be >= 0")
    task_dec = (
        Decimal(str(default_task_cap_usd))
        if default_task_cap_usd is not None
        else None
    )
    if task_dec is not None and task_dec <= 0:
        raise ValueError("default_task_cap_usd must be > 0 when set")

    await upsert_budget_config(user_id, cap_dec, task_dec, timezone_str)
    return await get_budget_settings(user_id)


async def put_scoped_task_cap(
    user_id: str,
    scope_kind: str,
    scope_id: str,
    cap_usd: float | Decimal,
) -> dict[str, Any]:
    if scope_kind not in ("workspace", "thread"):
        raise ValueError("scope_kind must be 'workspace' or 'thread'")
    cap_dec = Decimal(str(cap_usd))
    if cap_dec <= 0:
        raise ValueError("cap_usd must be > 0")
    if not await verify_task_cap_scope_owner(user_id, scope_kind, scope_id):
        raise ValueError(f"{scope_kind} {scope_id} does not belong to user {user_id}")
    row = await upsert_task_cap(user_id, scope_kind, scope_id, cap_dec)
    return {
        "scope_kind": row["scope_kind"],
        "scope_id": str(row["scope_id"]),
        "cap_usd": float(Decimal(str(row["cap_usd"]))),
    }


async def remove_scoped_task_cap(user_id: str, scope_kind: str, scope_id: str) -> bool:
    return await delete_task_cap(user_id, scope_kind, scope_id)
