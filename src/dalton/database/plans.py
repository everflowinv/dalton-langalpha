"""Database access layer for Dalton Research Director daily plans (d006, d008)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection


def _coerce_date(d: date | str) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return date.fromisoformat(str(d)[:10])


def _require_user_id(user_id: Optional[str]) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    return uid


def _verify_snapshot_owner(board_snapshot: Any, user_id: str) -> None:
    if not isinstance(board_snapshot, dict):
        raise HTTPException(
            status_code=403,
            detail="Daily plan snapshot has no owner or belongs to another user",
        )
    snap_owner = str(board_snapshot.get("user_id") or "").strip()
    if not snap_owner or snap_owner != user_id:
        raise HTTPException(
            status_code=403,
            detail="Daily plan snapshot has no owner or belongs to another user",
        )


def _verify_existing_plan_owner(existing: dict[str, Any], user_id: str) -> None:
    existing_row_owner = str(existing.get("user_id") or "").strip()
    existing_snap = existing.get("board_snapshot")
    existing_snap_owner = (
        str(existing_snap.get("user_id") or "").strip()
        if isinstance(existing_snap, dict)
        else ""
    )
    if (
        not existing_snap_owner
        or existing_snap_owner != user_id
        or (existing_row_owner and existing_row_owner != user_id)
    ):
        raise HTTPException(
            status_code=403,
            detail="Daily plan for this date has no owner in snapshot or belongs to another user",
        )


async def upsert_daily_plan(
    *,
    user_id: str,
    plan_date: date | str,
    budget_snapshot: dict[str, Any],
    board_snapshot: dict[str, Any],
    dispatches: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    pm_attention: list[dict[str, Any]],
    director_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically verify ownership and insert/update a Research Director daily plan for ``(user_id, plan_date)``."""
    uid = _require_user_id(user_id)
    _verify_snapshot_owner(board_snapshot, uid)
    p_date = _coerce_date(plan_date)
    thread_uuid = uuid.UUID(str(director_thread_id)) if director_thread_id else None
    board_snap = dict(board_snapshot)

    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, plan_date, budget_snapshot, board_snapshot,
                       dispatches, skipped, pm_attention, director_thread_id, created_at
                FROM dalton_daily_plans
                WHERE user_id = %s AND plan_date = %s
                FOR UPDATE
                """,
                (uid, p_date),
            )
            existing_raw = await cur.fetchone()
            if existing_raw is not None:
                existing = dict(existing_raw)
                _verify_existing_plan_owner(existing, uid)
                if "revisions" not in board_snap:
                    revs = list((existing.get("board_snapshot") or {}).get("revisions") or [])
                    revs.append(
                        {
                            "replaced_at": datetime.now(timezone.utc).isoformat(),
                            "dispatches": existing.get("dispatches") or [],
                        }
                    )
                    board_snap["revisions"] = revs

            await cur.execute(
                """
                INSERT INTO dalton_daily_plans (
                    user_id, plan_date, budget_snapshot, board_snapshot,
                    dispatches, skipped, pm_attention, director_thread_id, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, plan_date) DO UPDATE
                SET budget_snapshot = EXCLUDED.budget_snapshot,
                    board_snapshot = EXCLUDED.board_snapshot,
                    dispatches = EXCLUDED.dispatches,
                    skipped = EXCLUDED.skipped,
                    pm_attention = EXCLUDED.pm_attention,
                    director_thread_id = COALESCE(EXCLUDED.director_thread_id, dalton_daily_plans.director_thread_id)
                WHERE dalton_daily_plans.user_id = EXCLUDED.user_id
                  AND COALESCE(NULLIF(BTRIM(dalton_daily_plans.board_snapshot->>'user_id'), ''), '') = EXCLUDED.user_id
                RETURNING user_id, plan_date, budget_snapshot, board_snapshot,
                          dispatches, skipped, pm_attention, director_thread_id, created_at
                """,
                (
                    uid,
                    p_date,
                    Jsonb(budget_snapshot),
                    Jsonb(board_snap),
                    Jsonb(dispatches),
                    Jsonb(skipped),
                    Jsonb(pm_attention),
                    thread_uuid,
                ),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=403,
                    detail="Daily plan for this date has no owner in snapshot or belongs to another user",
                )
            return dict(row)


async def get_daily_plan(
    plan_date: date | str,
    *,
    user_id: str,
) -> Optional[dict[str, Any]]:
    """Retrieve a user's daily plan by ``(user_id, plan_date)``."""
    uid = _require_user_id(user_id)
    p_date = _coerce_date(plan_date)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, plan_date, budget_snapshot, board_snapshot,
                       dispatches, skipped, pm_attention, director_thread_id, created_at
                FROM dalton_daily_plans
                WHERE user_id = %s AND plan_date = %s
                """,
                (uid, p_date),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_daily_plans(
    *,
    user_id: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """List recent daily plans for ``user_id`` ordered by plan_date descending."""
    uid = _require_user_id(user_id)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, plan_date, budget_snapshot, board_snapshot,
                       dispatches, skipped, pm_attention, director_thread_id, created_at
                FROM dalton_daily_plans
                WHERE user_id = %s
                ORDER BY plan_date DESC
                LIMIT %s
                """,
                (uid, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
