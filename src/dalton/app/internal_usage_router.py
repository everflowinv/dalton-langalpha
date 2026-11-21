"""Internal read-only token usage aggregation endpoint for Dalton data tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.llm.antigravity_series import build_antigravity_series_catalog
from src.llms.llm import LLM
from src.server.database import pool

MAX_USAGE_WINDOW = timedelta(days=31)
USAGE_BUCKET_TIMEZONE = "UTC"

internal_usage_router = APIRouter(
    prefix="/internal/usage",
    tags=["Dalton Internal Usage"],
)

BY_MODEL_AGGREGATION_SQL = """
WITH filtered AS (
    SELECT
        u.conversation_usage_id,
        u.token_usage->'by_model' AS by_model,
        u.token_usage->'per_call_costs' AS per_call_costs
    FROM conversation_usages u
    WHERE u.created_at >= %(from_dt)s
      AND u.created_at < %(to_dt)s
      AND (
          jsonb_typeof(u.token_usage->'by_model') = 'object'
          OR (
              jsonb_typeof(u.token_usage->'per_call_costs') = 'array'
              AND jsonb_array_length(u.token_usage->'per_call_costs') > 0
          )
      )
),
raw_entries AS (
    SELECT
        f.conversation_usage_id,
        c->>'model_name' AS model,
        NULLIF(BTRIM(COALESCE(c->>'pricing_provider', '')), '') AS pricing_provider,
        1::bigint AS calls,
        GREATEST(
            COALESCE((c->>'input_tokens')::bigint, 0)
            - COALESCE((c->>'cached_tokens')::bigint, 0),
            0
        )::bigint AS fresh_input,
        COALESCE((c->>'cached_tokens')::bigint, 0)::bigint AS cache_read,
        COALESCE((c->>'cache_write_tokens')::bigint, 0)::bigint AS cache_write,
        COALESCE((c->>'output_tokens')::bigint, 0)::bigint AS output,
        COALESCE(
            (c->>'total_tokens')::bigint,
            COALESCE((c->>'input_tokens')::bigint, 0) + COALESCE((c->>'output_tokens')::bigint, 0)
        )::bigint AS total_tokens
    FROM filtered f,
         jsonb_array_elements(f.per_call_costs) AS c
    WHERE jsonb_typeof(f.per_call_costs) = 'array'
      AND jsonb_array_length(f.per_call_costs) > 0
      AND NULLIF(BTRIM(COALESCE(c->>'model_name', '')), '') IS NOT NULL
    UNION ALL
    SELECT
        f.conversation_usage_id,
        m.key AS model,
        NULL::text AS pricing_provider,
        COALESCE((m.value->>'call_count')::bigint, 0)::bigint AS calls,
        GREATEST(
            COALESCE((m.value->>'input_tokens')::bigint, 0)
            - COALESCE((m.value->>'cached_tokens')::bigint, 0),
            0
        )::bigint AS fresh_input,
        COALESCE((m.value->>'cached_tokens')::bigint, 0)::bigint AS cache_read,
        COALESCE((m.value->>'cache_write_tokens')::bigint, 0)::bigint AS cache_write,
        COALESCE((m.value->>'output_tokens')::bigint, 0)::bigint AS output,
        COALESCE((m.value->>'total_tokens')::bigint, 0)::bigint AS total_tokens
    FROM filtered f,
         jsonb_each(f.by_model) AS m
    WHERE NOT (
        jsonb_typeof(f.per_call_costs) = 'array'
        AND jsonb_array_length(f.per_call_costs) > 0
    )
      AND jsonb_typeof(f.by_model) = 'object'
),
per_model AS (
    SELECT
        r.model,
        r.pricing_provider,
        COALESCE(SUM(r.calls), 0)::bigint AS calls,
        COALESCE(SUM(r.fresh_input), 0)::bigint AS fresh_input,
        COALESCE(SUM(r.cache_read), 0)::bigint AS cache_read,
        COALESCE(SUM(r.cache_write), 0)::bigint AS cache_write,
        COALESCE(SUM(r.output), 0)::bigint AS output,
        COALESCE(SUM(r.total_tokens), 0)::bigint AS total_tokens,
        COUNT(DISTINCT r.conversation_usage_id)::bigint AS runs
    FROM raw_entries r
    GROUP BY r.model, r.pricing_provider
)
SELECT
    pm.model,
    pm.pricing_provider,
    pm.calls,
    pm.fresh_input,
    pm.cache_read,
    pm.cache_write,
    pm.output,
    pm.total_tokens,
    pm.runs,
    (SELECT COUNT(DISTINCT conversation_usage_id)::bigint FROM filtered) AS total_runs
FROM per_model pm
ORDER BY pm.total_tokens DESC, pm.model ASC, pm.pricing_provider ASC NULLS LAST
"""

# Bucket by UTC calendar date so daily aggregates align with prices.json effective_from
# and model_usage_cost_cli._date_for_timestamp (both interpret timestamps in UTC).
BY_MODEL_DAILY_AGGREGATION_SQL = """
WITH filtered AS (
    SELECT
        u.conversation_usage_id,
        ((u.created_at AT TIME ZONE 'UTC')::date)::text AS bucket_date,
        u.token_usage->'by_model' AS by_model,
        u.token_usage->'per_call_costs' AS per_call_costs
    FROM conversation_usages u
    WHERE u.created_at >= %(from_dt)s
      AND u.created_at < %(to_dt)s
      AND (
          jsonb_typeof(u.token_usage->'by_model') = 'object'
          OR (
              jsonb_typeof(u.token_usage->'per_call_costs') = 'array'
              AND jsonb_array_length(u.token_usage->'per_call_costs') > 0
          )
      )
),
raw_entries AS (
    SELECT
        f.conversation_usage_id,
        f.bucket_date AS date,
        c->>'model_name' AS model,
        NULLIF(BTRIM(COALESCE(c->>'pricing_provider', '')), '') AS pricing_provider,
        1::bigint AS calls,
        GREATEST(
            COALESCE((c->>'input_tokens')::bigint, 0)
            - COALESCE((c->>'cached_tokens')::bigint, 0),
            0
        )::bigint AS fresh_input,
        COALESCE((c->>'cached_tokens')::bigint, 0)::bigint AS cache_read,
        COALESCE((c->>'cache_write_tokens')::bigint, 0)::bigint AS cache_write,
        COALESCE((c->>'output_tokens')::bigint, 0)::bigint AS output,
        COALESCE(
            (c->>'total_tokens')::bigint,
            COALESCE((c->>'input_tokens')::bigint, 0) + COALESCE((c->>'output_tokens')::bigint, 0)
        )::bigint AS total_tokens
    FROM filtered f,
         jsonb_array_elements(f.per_call_costs) AS c
    WHERE jsonb_typeof(f.per_call_costs) = 'array'
      AND jsonb_array_length(f.per_call_costs) > 0
      AND NULLIF(BTRIM(COALESCE(c->>'model_name', '')), '') IS NOT NULL
    UNION ALL
    SELECT
        f.conversation_usage_id,
        f.bucket_date AS date,
        m.key AS model,
        NULL::text AS pricing_provider,
        COALESCE((m.value->>'call_count')::bigint, 0)::bigint AS calls,
        GREATEST(
            COALESCE((m.value->>'input_tokens')::bigint, 0)
            - COALESCE((m.value->>'cached_tokens')::bigint, 0),
            0
        )::bigint AS fresh_input,
        COALESCE((m.value->>'cached_tokens')::bigint, 0)::bigint AS cache_read,
        COALESCE((m.value->>'cache_write_tokens')::bigint, 0)::bigint AS cache_write,
        COALESCE((m.value->>'output_tokens')::bigint, 0)::bigint AS output,
        COALESCE((m.value->>'total_tokens')::bigint, 0)::bigint AS total_tokens
    FROM filtered f,
         jsonb_each(f.by_model) AS m
    WHERE NOT (
        jsonb_typeof(f.per_call_costs) = 'array'
        AND jsonb_array_length(f.per_call_costs) > 0
    )
      AND jsonb_typeof(f.by_model) = 'object'
),
per_model AS (
    SELECT
        r.date,
        r.model,
        r.pricing_provider,
        COALESCE(SUM(r.calls), 0)::bigint AS calls,
        COALESCE(SUM(r.fresh_input), 0)::bigint AS fresh_input,
        COALESCE(SUM(r.cache_read), 0)::bigint AS cache_read,
        COALESCE(SUM(r.cache_write), 0)::bigint AS cache_write,
        COALESCE(SUM(r.output), 0)::bigint AS output,
        COALESCE(SUM(r.total_tokens), 0)::bigint AS total_tokens,
        COUNT(DISTINCT r.conversation_usage_id)::bigint AS runs
    FROM raw_entries r
    GROUP BY r.date, r.model, r.pricing_provider
)
SELECT
    pm.date,
    pm.model,
    pm.pricing_provider,
    pm.calls,
    pm.fresh_input,
    pm.cache_read,
    pm.cache_write,
    pm.output,
    pm.total_tokens,
    pm.runs,
    (SELECT COUNT(DISTINCT conversation_usage_id)::bigint FROM filtered) AS total_runs
FROM per_model pm
ORDER BY pm.date ASC, pm.total_tokens DESC, pm.model ASC, pm.pricing_provider ASC NULLS LAST
"""


class ModelUsageAggregate(BaseModel):
    model: str
    calls: int
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    total_tokens: int
    runs: int


class UsageAggregateTotals(BaseModel):
    calls: int
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    total_tokens: int
    runs: int


class UsageByModelResponse(BaseModel):
    window_from: str
    window_to: str
    input_includes_cached: bool = True
    models: list[ModelUsageAggregate]
    totals: UsageAggregateTotals

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        # Expose `from`/`to` in serialized payloads without reserved keyword clashes.
        data = super().model_dump(**kwargs)
        data["from"] = data.pop("window_from")
        data["to"] = data.pop("window_to")
        return data


def _parse_iso_utc(raw: str, *, field_name: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO datetime for '{field_name}'",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


async def fetch_usage_by_model(
    conn: Any,
    *,
    from_dt: datetime,
    to_dt: datetime,
    bucket: str = "total",
) -> dict[str, Any]:
    """Run the single-SQL per-model aggregation on ``conversation_usages`` for [from_dt, to_dt)."""
    if bucket not in ("total", "day"):
        raise ValueError(f"Unsupported usage bucket: {bucket}")
    sql = BY_MODEL_DAILY_AGGREGATION_SQL if bucket == "day" else BY_MODEL_AGGREGATION_SQL

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            sql,
            {"from_dt": from_dt, "to_dt": to_dt},
        )
        rows = await cur.fetchall()

    models: list[dict[str, Any]] = []
    tot_calls = 0
    tot_fresh = 0
    tot_cache_read = 0
    tot_cache_write = 0
    tot_output = 0
    tot_total = 0
    distinct_runs: int | None = None

    for row in rows:
        calls = int(row.get("calls") or 0)
        fresh_input = int(row.get("fresh_input") or 0)
        cache_read = int(row.get("cache_read") or 0)
        cache_write = int(row.get("cache_write") or 0)
        output = int(row.get("output") or 0)
        total_tokens = int(row.get("total_tokens") or 0)
        runs = int(row.get("runs") or 0)
        if row.get("total_runs") is not None:
            distinct_runs = int(row["total_runs"])

        item: dict[str, Any] = {
            "model": str(row.get("model") or ""),
            "calls": calls,
            "fresh_input": fresh_input,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": output,
            "total_tokens": total_tokens,
            "runs": runs,
        }
        if "pricing_provider" in row:
            raw_pp = row.get("pricing_provider")
            pp = str(raw_pp).strip() if isinstance(raw_pp, str) else ""
            item["pricing_provider"] = pp or "unknown"
        if row.get("date") is not None:
            item["date"] = str(row["date"])[:10]
        models.append(item)
        tot_calls += calls
        tot_fresh += fresh_input
        tot_cache_read += cache_read
        tot_cache_write += cache_write
        tot_output += output
        tot_total += total_tokens

    payload: dict[str, Any] = {
        "from": from_dt.isoformat().replace("+00:00", "Z"),
        "to": to_dt.isoformat().replace("+00:00", "Z"),
        "bucket": bucket,
        "input_includes_cached": True,
        "models": models,
        "antigravity_series": build_antigravity_series_catalog(LLM.get_model_config()),
        "totals": {
            "calls": tot_calls,
            "fresh_input": tot_fresh,
            "cache_read": tot_cache_read,
            "cache_write": tot_cache_write,
            "output": tot_output,
            "total_tokens": tot_total,
            "runs": distinct_runs if distinct_runs is not None else sum(m["runs"] for m in models),
        },
    }
    if bucket == "day":
        payload["timezone"] = USAGE_BUCKET_TIMEZONE
    return payload


@internal_usage_router.get("/by-model")
async def get_usage_by_model_endpoint(
    from_raw: str = Query(..., alias="from", min_length=1),
    to_raw: str = Query(..., alias="to", min_length=1),
    bucket: str = Query("total"),
    _: None = Depends(require_channel_service_token),
) -> dict[str, Any]:
    """Return read-only per-model token totals from ``conversation_usages`` for [from, to)."""
    if bucket not in ("total", "day"):
        raise HTTPException(
            status_code=400,
            detail="Invalid 'bucket', expected 'total' or 'day'",
        )
    from_dt = _parse_iso_utc(from_raw, field_name="from")
    to_dt = _parse_iso_utc(to_raw, field_name="to")
    if from_dt >= to_dt:
        raise HTTPException(
            status_code=400,
            detail="'from' must be earlier than 'to'",
        )
    if (to_dt - from_dt) > MAX_USAGE_WINDOW:
        raise HTTPException(
            status_code=400,
            detail="Time window exceeds maximum allowed 31 days",
        )

    async with pool.get_db_connection() as conn:
        return await fetch_usage_by_model(conn, from_dt=from_dt, to_dt=to_dt, bucket=bucket)
