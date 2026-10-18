"""Database access layer for Dalton Research Director daily plans (d006)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection


def _coerce_date(d: date | str) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return date.fromisoformat(str(d)[:10])


async def upsert_daily_plan(
    *,
    plan_date: date | str,
    budget_snapshot: dict[str, Any],
    board_snapshot: dict[str, Any],
    dispatches: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    pm_attention: list[dict[str, Any]],
    director_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or update a Research Director daily plan for ``plan_date``."""
    p_date = _coerce_date(plan_date)
    thread_uuid = uuid.UUID(str(director_thread_id)) if director_thread_id else None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_daily_plans (
                    plan_date, budget_snapshot, board_snapshot,
                    dispatches, skipped, pm_attention, director_thread_id, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (plan_date) DO UPDATE
                SET budget_snapshot = EXCLUDED.budget_snapshot,
                    board_snapshot = EXCLUDED.board_snapshot,
                    dispatches = EXCLUDED.dispatches,
                    skipped = EXCLUDED.skipped,
                    pm_attention = EXCLUDED.pm_attention,
                    director_thread_id = COALESCE(EXCLUDED.director_thread_id, dalton_daily_plans.director_thread_id)
                RETURNING plan_date, budget_snapshot, board_snapshot,
                          dispatches, skipped, pm_attention, director_thread_id, created_at
                """,
                (
                    p_date,
                    Jsonb(budget_snapshot),
                    Jsonb(board_snapshot),
                    Jsonb(dispatches),
                    Jsonb(skipped),
                    Jsonb(pm_attention),
                    thread_uuid,
                ),
            )
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_daily_plan(plan_date: date | str) -> Optional[dict[str, Any]]:
    """Retrieve a daily plan by date."""
    p_date = _coerce_date(plan_date)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT plan_date, budget_snapshot, board_snapshot,
                       dispatches, skipped, pm_attention, director_thread_id, created_at
                FROM dalton_daily_plans
                WHERE plan_date = %s
                """,
                (p_date,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_daily_plans(limit: int = 30) -> list[dict[str, Any]]:
    """List recent daily plans ordered by plan_date descending."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT plan_date, budget_snapshot, board_snapshot,
                       dispatches, skipped, pm_attention, director_thread_id, created_at
                FROM dalton_daily_plans
                ORDER BY plan_date DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
