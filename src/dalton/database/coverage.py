"""Raw psycopg3 SQL queries for per-user coverage roster, groups, and earnings calendar mirror."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from psycopg.rows import dict_row

from src.dalton.coverage.models import extract_row_group_ids
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


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

    async with get_db_connection(conn) as c:
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


async def list_all_coverage_rows(*, conn: Any = None) -> list[dict[str, Any]]:
    """Return all `dalton_coverage` rows across all users (used for union alias fallback)."""
    async with get_db_connection(conn) as c:
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

    async with get_db_connection(conn) as c:
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


async def count_coverage_rows(user_id: str, *, conn: Any = None) -> int:
    """Count `dalton_coverage` rows for `user_id`."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT COUNT(*)::int AS cnt FROM dalton_coverage WHERE user_id = %s",
                (uid,),
            )
            row = await cur.fetchone()
            return int((row or {}).get("cnt") or 0)


async def upsert_coverage_rows(
    user_id: str,
    rows: list[dict[str, Any]],
    *,
    expected_unique_count: int | None = None,
    conn: Any = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Upsert coverage rows for `user_id` under per-user advisory lock.

    When `expected_unique_count` is provided, verifies inside the same transaction
    that the unique upserted ticker count matches `expected_unique_count`, rolling
    back the transaction if the assertion fails.
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
                    SET name = CASE
                            WHEN EXCLUDED.name <> '' THEN EXCLUDED.name
                            ELSE dalton_coverage.name
                        END,
                        tier = CASE
                            WHEN EXCLUDED.source = 'group' THEN dalton_coverage.tier
                            ELSE EXCLUDED.tier
                        END,
                        market = CASE
                            WHEN EXCLUDED.market <> '' THEN EXCLUDED.market
                            ELSE dalton_coverage.market
                        END,
                        peer_group = CASE
                            WHEN EXCLUDED.peer_group <> '' THEN EXCLUDED.peer_group
                            ELSE dalton_coverage.peer_group
                        END,
                        peers = EXCLUDED.peers,
                        aliases = EXCLUDED.aliases,
                        earnings = EXCLUDED.earnings,
                        extra = EXCLUDED.extra,
                        source = CASE
                            WHEN EXCLUDED.source = 'group' AND dalton_coverage.source <> 'group'
                                THEN dalton_coverage.source
                            ELSE EXCLUDED.source
                        END,
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

            if expected_unique_count is not None:
                unique_tickers = {str(r["ticker"]).strip().upper() for r in upserted}
                await cur.execute(
                    """
                    SELECT COUNT(DISTINCT ticker)::int AS cnt
                    FROM dalton_coverage
                    WHERE user_id = %s AND ticker = ANY(%s)
                    """,
                    (uid, list(unique_tickers)),
                )
                cnt_row = await cur.fetchone()
                db_unique_count = int((cnt_row or {}).get("cnt") or 0)
                if (
                    len(upserted) != expected_unique_count
                    or len(unique_tickers) != expected_unique_count
                    or db_unique_count != expected_unique_count
                ):
                    raise AssertionError(
                        f"Written unique row count (upserted={len(upserted)}, "
                        f"unique={len(unique_tickers)}, db={db_unique_count}) "
                        f"does not match expected company count ({expected_unique_count})"
                    )
        return prev_count, upserted

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
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
    async with get_db_connection() as c:
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
    async with get_db_connection() as c:
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
    async with get_db_connection() as c:
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

    async with get_db_connection(conn) as c:
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

    async with get_db_connection(conn) as c:
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
    async with get_db_connection() as c:
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
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def delete_coverage_group_and_exclusive_members(
    user_id: str,
    group_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Delete a coverage group and remove only those coverage entries that belong to no other group and were not manually added."""
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        return {"deleted": False, "group_id": gid, "removed_tickers": []}

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        await acquire_coverage_advisory_lock(c, uid)
        existing_groups = await list_coverage_groups(uid, conn=c)
        valid_other_group_ids = {
            str(g["group_id"]).strip()
            for g in existing_groups
            if g.get("group_id") and str(g["group_id"]).strip() != gid
        }
        rows = await list_coverage_rows(uid, conn=c)
        to_delete_tickers: list[str] = []
        to_update_rows: list[tuple[str, str | None, str]] = []

        for r in rows:
            gids = extract_row_group_ids(r)
            if gid not in gids and str(r.get("group_id") or "") != gid:
                continue
            remaining = [g for g in gids if g != gid and g in valid_other_group_ids]
            src = str(r.get("source") or "chat").strip().lower()
            sym = str(r["ticker"]).strip().upper()
            if src == "group" and not remaining:
                to_delete_tickers.append(sym)
            else:
                new_extra = dict(r.get("extra") or {})
                if remaining:
                    new_extra["group_ids"] = remaining
                    new_primary: str | None = remaining[0]
                else:
                    new_extra.pop("group_ids", None)
                    new_primary = None
                to_update_rows.append(
                    (
                        sym,
                        new_primary,
                        json.dumps(new_extra, ensure_ascii=False),
                    )
                )

        removed_tickers: list[str] = []
        async with c.cursor(row_factory=dict_row) as cur:
            if to_delete_tickers:
                await cur.execute(
                    """
                    DELETE FROM dalton_coverage
                    WHERE user_id = %s AND ticker = ANY(%s)
                    RETURNING ticker
                    """,
                    (uid, to_delete_tickers),
                )
                del_rows = await cur.fetchall()
                removed_tickers = [str(r["ticker"]) for r in del_rows if r and r.get("ticker")]

            for sym, new_primary, new_extra_json in to_update_rows:
                await cur.execute(
                    """
                    UPDATE dalton_coverage
                    SET group_id = %s::uuid,
                        extra = %s::jsonb,
                        updated_at = NOW()
                    WHERE user_id = %s AND ticker = %s
                    """,
                    (new_primary, new_extra_json, uid, sym),
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
    async with get_db_connection() as c:
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

    async with get_db_connection(conn) as c:
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
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)
