"""Raw psycopg3 SQL queries for per-user coverage roster, groups, and earnings calendar mirror."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Mapping

from psycopg.rows import dict_row

from src.server.database import pool as _pool_mod
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_ORIG_GET_DB_CONNECTION = get_db_connection


@asynccontextmanager
async def _open_db_connection():
    """Resolve `get_db_connection` respecting monkeypatches on either this module or `pool`."""
    fn = (
        get_db_connection
        if get_db_connection is not _ORIG_GET_DB_CONNECTION
        else _pool_mod.get_db_connection
    )
    async with fn() as conn:
        yield conn


def _normalize_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("group_id") is not None:
        out["group_id"] = str(out["group_id"])
    for json_col, default in (
        ("peers", []),
        ("aliases", {}),
        ("earnings", {}),
        ("extra", {}),
        ("criteria", {}),
        ("ledger", {}),
    ):
        if json_col in out and isinstance(out[json_col], str):
            try:
                out[json_col] = json.loads(out[json_col])
            except Exception:
                out[json_col] = default
    return out


async def acquire_coverage_advisory_lock(conn: Any, user_id: str) -> None:
    """Acquire transaction-scoped advisory lock for a user's coverage mutations."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty for coverage advisory lock")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext('dalton_coverage'), hashtext(%s))",
            (uid,),
        )


async def list_coverage_rows(
    user_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return all `dalton_coverage` rows for `user_id` ordered by `added_at ASC, ticker ASC`."""
    uid = str(user_id or "").strip()
    if not uid:
        return []

    async def _run(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    user_id, ticker, name, tier, market, peer_group,
                    peers, aliases, earnings, extra, source,
                    group_id::text AS group_id, added_at, updated_at
                FROM dalton_coverage
                WHERE user_id = %s
                ORDER BY added_at ASC, ticker ASC
                """,
                (uid,),
            )
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def list_all_coverage_rows(*, conn: Any = None) -> list[dict[str, Any]]:
    """Return all `dalton_coverage` rows across all users (used for union alias fallback)."""

    async def _run(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    user_id, ticker, name, tier, market, peer_group,
                    peers, aliases, earnings, extra, source,
                    group_id::text AS group_id, added_at, updated_at
                FROM dalton_coverage
                ORDER BY user_id ASC, added_at ASC, ticker ASC
                """
            )
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def get_coverage_row(
    user_id: str,
    ticker: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return a single `dalton_coverage` row for `(user_id, ticker)` or None."""
    uid = str(user_id or "").strip()
    sym = str(ticker or "").strip().upper()
    if not uid or not sym:
        return None

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    user_id, ticker, name, tier, market, peer_group,
                    peers, aliases, earnings, extra, source,
                    group_id::text AS group_id, added_at, updated_at
                FROM dalton_coverage
                WHERE user_id = %s AND ticker = %s
                """,
                (uid, sym),
            )
            return _normalize_row(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def count_coverage_rows(user_id: str, *, conn: Any = None) -> int:
    """Count `dalton_coverage` rows for `user_id`."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0

    async def _run(c: Any) -> int:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT COUNT(*)::int AS cnt FROM dalton_coverage WHERE user_id = %s",
                (uid,),
            )
            row = await cur.fetchone()
            return int((row or {}).get("cnt") or 0)

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def upsert_coverage_rows(
    user_id: str,
    rows: list[dict[str, Any]],
    *,
    conn: Any = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Upsert coverage rows for `user_id` under per-user advisory lock.

    Returns `(previous_count, upserted_rows)`.
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty")

    async def _run_in_tx(c: Any) -> tuple[int, list[dict[str, Any]]]:
        await acquire_coverage_advisory_lock(c, uid)
        prev_count = await count_coverage_rows(uid, conn=c)
        upserted: list[dict[str, Any]] = []
        async with c.cursor(row_factory=dict_row) as cur:
            for idx, r in enumerate(rows):
                ticker = str(r["ticker"]).strip().upper()
                name = str(r.get("name") or "")
                tier = str(r.get("tier") or "watch").strip().lower()
                market = str(r.get("market") or "US")
                peer_group = str(r.get("peer_group") or "")
                peers_json = json.dumps(r.get("peers") or [], ensure_ascii=False)
                aliases_json = json.dumps(r.get("aliases") or {}, ensure_ascii=False)
                earnings_json = json.dumps(r.get("earnings") or {}, ensure_ascii=False)
                extra_json = json.dumps(r.get("extra") or {}, ensure_ascii=False)
                source = str(r.get("source") or "chat").strip().lower()
                group_id = str(r["group_id"]) if r.get("group_id") else None

                await cur.execute(
                    """
                    INSERT INTO dalton_coverage (
                        user_id, ticker, name, tier, market, peer_group,
                        peers, aliases, earnings, extra, source, group_id,
                        added_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::uuid,
                        NOW() + (%s * INTERVAL '1 microsecond'), NOW()
                    )
                    ON CONFLICT (user_id, ticker) DO UPDATE
                    SET name = EXCLUDED.name,
                        tier = EXCLUDED.tier,
                        market = EXCLUDED.market,
                        peer_group = EXCLUDED.peer_group,
                        peers = EXCLUDED.peers,
                        aliases = EXCLUDED.aliases,
                        earnings = EXCLUDED.earnings,
                        extra = EXCLUDED.extra,
                        source = EXCLUDED.source,
                        group_id = COALESCE(EXCLUDED.group_id, dalton_coverage.group_id),
                        updated_at = NOW()
                    RETURNING
                        user_id, ticker, name, tier, market, peer_group,
                        peers, aliases, earnings, extra, source,
                        group_id::text AS group_id, added_at, updated_at
                    """,
                    (
                        uid,
                        ticker,
                        name,
                        tier,
                        market,
                        peer_group,
                        peers_json,
                        aliases_json,
                        earnings_json,
                        extra_json,
                        source,
                        group_id,
                        idx,
                    ),
                )
                fetched = await cur.fetchone()
                if fetched is not None:
                    norm = _normalize_row(fetched)
                    if norm is not None:
                        upserted.append(norm)
        return prev_count, upserted

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def delete_coverage_tickers(
    user_id: str,
    tickers: list[str],
    *,
    conn: Any = None,
) -> list[str]:
    """Delete tickers from `dalton_coverage` for `user_id` under per-user advisory lock."""
    uid = str(user_id or "").strip()
    cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not uid or not cleaned:
        return []

    async def _run_in_tx(c: Any) -> list[str]:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM dalton_coverage
                WHERE user_id = %s AND ticker = ANY(%s)
                RETURNING ticker
                """,
                (uid, cleaned),
            )
            rows = await cur.fetchall()
            return [str(r["ticker"]) for r in rows if r and r.get("ticker")]

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def update_coverage_tier(
    user_id: str,
    tickers: list[str],
    tier: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Update `tier` for `tickers` in `dalton_coverage` for `user_id` under per-user advisory lock."""
    uid = str(user_id or "").strip()
    cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not uid or not cleaned:
        return []

    async def _run_in_tx(c: Any) -> list[dict[str, Any]]:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_coverage
                SET tier = %s,
                    updated_at = NOW()
                WHERE user_id = %s AND ticker = ANY(%s)
                RETURNING
                    user_id, ticker, name, tier, market, peer_group,
                    peers, aliases, earnings, extra, source,
                    group_id::text AS group_id, added_at, updated_at
                """,
                (tier, uid, cleaned),
            )
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def mark_coverage_rows_group_stale(
    user_id: str,
    tickers: list[str],
    *,
    stale: bool = True,
    conn: Any = None,
) -> list[str]:
    """Set or clear `extra.group_stale` on specified tickers for `user_id` under advisory lock."""
    uid = str(user_id or "").strip()
    cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not uid or not cleaned:
        return []

    async def _run_in_tx(c: Any) -> list[str]:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            if stale:
                await cur.execute(
                    """
                    UPDATE dalton_coverage
                    SET extra = COALESCE(extra, '{}'::jsonb) || '{"group_stale": true}'::jsonb,
                        updated_at = NOW()
                    WHERE user_id = %s AND ticker = ANY(%s)
                    RETURNING ticker
                    """,
                    (uid, cleaned),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_coverage
                    SET extra = COALESCE(extra, '{}'::jsonb) - 'group_stale',
                        updated_at = NOW()
                    WHERE user_id = %s AND ticker = ANY(%s)
                    RETURNING ticker
                    """,
                    (uid, cleaned),
                )
            rows = await cur.fetchall()
            return [str(r["ticker"]) for r in rows if r and r.get("ticker")]

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def list_coverage_groups(
    user_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List all `dalton_coverage_groups` rows for `user_id`."""
    uid = str(user_id or "").strip()
    if not uid:
        return []

    async def _run(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    group_id::text AS group_id,
                    user_id,
                    label,
                    criteria,
                    default_tier,
                    auto_refresh,
                    last_resolved_at,
                    created_at
                FROM dalton_coverage_groups
                WHERE user_id = %s
                ORDER BY created_at ASC
                """,
                (uid,),
            )
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def get_coverage_group(
    user_id: str,
    group_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single `dalton_coverage_groups` row belonging to `user_id`."""
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        return None

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    group_id::text AS group_id,
                    user_id,
                    label,
                    criteria,
                    default_tier,
                    auto_refresh,
                    last_resolved_at,
                    created_at
                FROM dalton_coverage_groups
                WHERE user_id = %s AND group_id = %s::uuid
                """,
                (uid, gid),
            )
            return _normalize_row(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def insert_coverage_group(
    user_id: str,
    *,
    label: str,
    criteria: Mapping[str, Any],
    default_tier: str = "watch",
    auto_refresh: bool = True,
    last_resolved_at: datetime | None = None,
    group_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert a new coverage group row for `user_id` under per-user advisory lock."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty")
    gid = str(group_id) if group_id else str(uuid.uuid4())
    resolved_ts = last_resolved_at or datetime.now(timezone.utc)
    criteria_json = json.dumps(dict(criteria), ensure_ascii=False)

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_coverage_groups (
                    group_id, user_id, label, criteria, default_tier,
                    auto_refresh, last_resolved_at, created_at
                )
                VALUES (%s::uuid, %s, %s, %s::jsonb, %s, %s, %s, NOW())
                RETURNING
                    group_id::text AS group_id,
                    user_id,
                    label,
                    criteria,
                    default_tier,
                    auto_refresh,
                    last_resolved_at,
                    created_at
                """,
                (gid, uid, label, criteria_json, default_tier, auto_refresh, resolved_ts),
            )
            row = await cur.fetchone()
            norm = _normalize_row(row)
            assert norm is not None
            return norm

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def update_coverage_group_resolved(
    user_id: str,
    group_id: str,
    *,
    last_resolved_at: datetime | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Update `last_resolved_at` on a coverage group for `user_id`."""
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        return None
    ts = last_resolved_at or datetime.now(timezone.utc)

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_coverage_groups
                SET last_resolved_at = %s
                WHERE user_id = %s AND group_id = %s::uuid
                RETURNING
                    group_id::text AS group_id,
                    user_id,
                    label,
                    criteria,
                    default_tier,
                    auto_refresh,
                    last_resolved_at,
                    created_at
                """,
                (ts, uid, gid),
            )
            return _normalize_row(await cur.fetchone())

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def delete_coverage_group_and_exclusive_members(
    user_id: str,
    group_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Delete a coverage group and any coverage entries added exclusively via that group (`source='group'`)."""
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        return {"deleted": False, "group_id": gid, "removed_tickers": []}

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        await acquire_coverage_advisory_lock(c, uid)
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM dalton_coverage
                WHERE user_id = %s AND group_id = %s::uuid AND source = 'group'
                RETURNING ticker
                """,
                (uid, gid),
            )
            del_rows = await cur.fetchall()
            removed_tickers = [str(r["ticker"]) for r in del_rows if r and r.get("ticker")]

            await cur.execute(
                """
                UPDATE dalton_coverage
                SET group_id = NULL, updated_at = NOW()
                WHERE user_id = %s AND group_id = %s::uuid
                """,
                (uid, gid),
            )

            await cur.execute(
                """
                DELETE FROM dalton_coverage_groups
                WHERE user_id = %s AND group_id = %s::uuid
                RETURNING group_id::text AS group_id, label
                """,
                (uid, gid),
            )
            grp_row = await cur.fetchone()
            return {
                "deleted": grp_row is not None,
                "group_id": gid,
                "label": (grp_row or {}).get("label"),
                "removed_tickers": removed_tickers,
            }

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def get_earnings_calendar_mirror(
    user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch the user's mirrored earnings calendar ledger row from `dalton_earnings_calendar_mirror`."""
    uid = str(user_id or "").strip()
    if not uid:
        return None

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, ledger, published_at
                FROM dalton_earnings_calendar_mirror
                WHERE user_id = %s
                """,
                (uid,),
            )
            return _normalize_row(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with _open_db_connection() as c:
        return await _run(c)


async def upsert_earnings_calendar_mirror(
    user_id: str,
    ledger: Mapping[str, Any],
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Upsert `dalton_earnings_calendar_mirror` row for `user_id`."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty")
    ledger_json = json.dumps(dict(ledger), ensure_ascii=False)

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_earnings_calendar_mirror (user_id, ledger, published_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET ledger = EXCLUDED.ledger,
                    published_at = NOW()
                RETURNING user_id, ledger, published_at
                """,
                (uid, ledger_json),
            )
            row = await cur.fetchone()
            norm = _normalize_row(row)
            assert norm is not None
            return norm

    if conn is not None:
        return await _run_in_tx(conn)
    async with _open_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)
