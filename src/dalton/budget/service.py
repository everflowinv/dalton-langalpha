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
from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_dalton_meta, load_overlay_data
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
    in_rate = float(np.get("input", 0.0) or 0.0)
    out_rate = float(np.get("output", 0.0) or 0.0)
    cache_rate = float(np.get("cached_input", 0.0) or 0.0)
    if not in_rate and isinstance(np.get("input_tiers"), list) and np["input_tiers"]:
        in_rate = float(np["input_tiers"][0].get("rate", 0.0) or 0.0)
        cache_rate = float(np["input_tiers"][0].get("cached_input", cache_rate) or 0.0)
    if not out_rate and isinstance(np.get("output_tiers"), list) and np["output_tiers"]:
        out_rate = float(np["output_tiers"][0].get("rate", 0.0) or 0.0)
    return {"input": in_rate, "output": out_rate, "cached_input": cache_rate}


def _notional_price_index() -> dict[str, dict[str, float]]:
    """Build `{model_key_or_ref: {'input': x, 'output': y, 'cached_input': z}}` for subscription/OAuth models."""
    out: dict[str, dict[str, float]] = {}
    overlay = load_overlay_data() or {}
    models_map = overlay.get("models") or {}
    prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
        "openclaw-broker", []
    )
    by_ref: dict[str, dict[str, Any]] = {}
    for pm in prov_models:
        if isinstance(pm, dict) and pm.get("id"):
            by_ref[str(pm["id"])] = pm

    for key, mcfg in models_map.items():
        if not isinstance(mcfg, dict):
            continue
        ref = str(mcfg.get("model_id") or "")
        pm = by_ref.get(ref) or {}
        np = pm.get("notional_pricing")
        if isinstance(np, dict):
            rates = _extract_rates_from_card(np)
            out[str(key)] = rates
            if ref:
                out[ref] = rates

    # P0N.6 §4: Also read load_dalton_meta() for subscription models (notional_pricing / notional_ref)
    try:
        from src.llms.pricing_utils import find_model_pricing

        dalton_meta = load_dalton_meta() or {}
        native_overlay = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
        native_models = native_overlay.get("models") or {}

        for key, meta_entry in dalton_meta.items():
            if not isinstance(meta_entry, dict):
                continue
            rates: dict[str, float] | None = None
            np = meta_entry.get("notional_pricing")
            if isinstance(np, dict):
                rates = _extract_rates_from_card(np)
            elif meta_entry.get("notional_ref"):
                ref_id = str(meta_entry["notional_ref"])
                if ref_id in out:
                    rates = out[ref_id]
                else:
                    card = find_model_pricing(ref_id)
                    if isinstance(card, dict):
                        rates = _extract_rates_from_card(card)
            if rates is not None:
                out[str(key)] = rates
                m_entry = native_models.get(key) if isinstance(native_models, dict) else None
                if isinstance(m_entry, dict) and m_entry.get("model_id"):
                    out.setdefault(str(m_entry["model_id"]), rates)
    except Exception as exc:
        logger.debug("[DaltonBudget] _notional_price_index dalton_meta lookup skipped: %s", exc)

    # Fallback nominal rates for common OAuth models when upstream card is absent
    out.setdefault(
        "claude-opus-5-5-oauth",
        {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    )
    out.setdefault(
        "claude-opus-5-5-high-oauth",
        {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    )
    out.setdefault(
        "gpt-6-astra-oauth",
        {"input": 10.0, "output": 50.0, "cached_input": 1.0},
    )
    out.setdefault(
        "gpt-6.1-sol-oauth",
        {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    )
    out.setdefault(
        "gemini-4-argon-high-oauth",
        {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    )
    out.setdefault(
        "gemini-3.8-flash-medium-oauth",
        {"input": 0.75, "output": 3.75, "cached_input": 0.075},
    )
    out.setdefault(
        "gemini-3.8-flash-low-oauth",
        {"input": 0.75, "output": 3.75, "cached_input": 0.075},
    )
    return out


def compute_subscription_notional_usd(token_usage_rows: list[dict[str, Any]]) -> Decimal:
    """Estimate nominal USD consumed today by subscription (`pricing_type=='subscription'`) and OAuth models."""
    idx = _notional_price_index()
    total = Decimal("0")
    for row in token_usage_rows:
        tu = row.get("token_usage")
        if not isinstance(tu, dict):
            continue
        by_model = tu.get("by_model")
        if not isinstance(by_model, dict):
            continue
        for model_name, stats in by_model.items():
            if not isinstance(stats, dict):
                continue
            rates_list = stats.get("rates") or []
            is_oauth = any(
                isinstance(r, dict) and r.get("billing_type") == "oauth"
                for r in rates_list
            )
            if is_oauth and stats.get("total_cost"):
                total += Decimal(str(stats["total_cost"]))
                continue

            card = idx.get(str(model_name))
            if card is not None and (is_oauth or float(stats.get("total_cost") or 0.0) == 0.0):
                in_tok = int(stats.get("input_tokens") or 0)
                out_tok = int(stats.get("output_tokens") or 0)
                cached_tok = int(stats.get("cached_tokens") or 0)
                uncached_in = max(0, in_tok - cached_tok)
                usd = (
                    uncached_in * card["input"]
                    + cached_tok * card["cached_input"]
                    + out_tok * card["output"]
                ) / 1_000_000.0
                total += Decimal(f"{usd:.6f}")
    return total.quantize(Decimal("0.000001"))


def _resolve_provider_for_model(model_name: str, billing_type: str = "platform") -> str:
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
                prov = _resolve_provider_for_model(m_name, b_type)
                b = _ensure(prov)
                b["calls"] += 1
                cost_dec = Decimal(str(c.get("cost") or "0"))
                if b_type == "oauth":
                    if cost_dec > Decimal("0"):
                        b["notional_usd"] += cost_dec
                    elif m_name in idx:
                        card = idx[m_name]
                        in_tok = int(c.get("input_tokens") or 0)
                        out_tok = int(c.get("output_tokens") or 0)
                        cached_tok = int(c.get("cached_tokens") or 0)
                        uncached = max(0, in_tok - cached_tok)
                        est = (
                            uncached * card["input"]
                            + cached_tok * card["cached_input"]
                            + out_tok * card["output"]
                        ) / 1_000_000.0
                        b["notional_usd"] += Decimal(f"{est:.6f}")
                else:
                    b["cash_usd"] += cost_dec

    for j in journal_rows or []:
        if not isinstance(j, dict) or j.get("state") not in ("done", "indeterminate"):
            continue
        m_name = str(j.get("model_key") or "unknown")
        b_type = str(j.get("billing_type") or "platform")
        prov = str(j.get("provider") or _resolve_provider_for_model(m_name, b_type))
        b = _ensure(prov)
        b["calls"] += 1
        b["cash_usd"] += Decimal(str(j.get("cash_usd") or "0"))
        b["notional_usd"] += Decimal(str(j.get("notional_usd") or "0"))

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


async def get_budget_today(user_id: str = "local-dev-user") -> dict[str, Any]:
    """Public read function (P0.8 §10): reconcile and return today's budget state.

    Used by `/api/v1/dalton/budget/today`, PK preflight, Research Director, and PS automations.
    """
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
