"""PostgreSQL persistence for Dalton data source health checks and capability-scoped daily data map cache (`d011`, `d046`).

Multi-worker invariant: all health status and daily rendered data map state live in PostgreSQL
(`dalton_data_source_health` and `dalton_data_map_scoped_cache`). The legacy `dalton_data_map_cache`
table is left untouched for rolling-upgrade compatibility with previous backends. Never cache health
or data map state in process-local module variables.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

VALID_HEALTH_STATUSES: frozenset[str] = frozenset({"available", "unavailable", "unprobed"})
HEALTH_ADVISORY_LOCK_KEY = "dalton_data_source_health_run"

CARD_TYPES_ORDER: tuple[str, ...] = (
    "fact",
    "estimate",
    "model_assumption",
    "opinion",
    "question",
    "decision",
    "todo",
)


async def try_acquire_health_run_lock(
    conn: Any = None,
    *,
    lock_key: str = HEALTH_ADVISORY_LOCK_KEY,
) -> bool:
    """Acquire a Postgres session-level advisory lock for the health check (`pg_try_advisory_lock(hashtext(%s))`)."""
    sql = "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked"
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (str(lock_key),))
            row = await cur.fetchone()
            if isinstance(row, Mapping):
                return bool(row.get("locked"))
            if isinstance(row, (tuple, list)) and row:
                return bool(row[0])
            return False


async def release_health_run_lock(
    conn: Any = None,
    *,
    lock_key: str = HEALTH_ADVISORY_LOCK_KEY,
) -> bool:
    """Release the Postgres session-level advisory lock acquired by `try_acquire_health_run_lock`."""
    sql = "SELECT pg_advisory_unlock(hashtext(%s)) AS unlocked"
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (str(lock_key),))
            row = await cur.fetchone()
            if isinstance(row, Mapping):
                return bool(row.get("unlocked"))
            if isinstance(row, (tuple, list)) and row:
                return bool(row[0])
            return False


def _normalize_status(status: Any) -> str:
    val = str(status or "unprobed").strip().lower()
    return val if val in VALID_HEALTH_STATUSES else "unprobed"


async def upsert_source_health(
    source_id: str,
    *,
    category: str = "",
    status: str = "unprobed",
    latency_ms: int | None = None,
    latest_date: str | None = None,
    error_type: str | None = None,
    detail: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Insert or update a single data source health row in `dalton_data_source_health`."""
    await upsert_source_health_batch(
        [
            {
                "source_id": source_id,
                "category": category,
                "status": status,
                "latency_ms": latency_ms,
                "latest_date": latest_date,
                "error_type": error_type,
                "detail": dict(detail or {}),
            }
        ],
        conn=conn,
    )


async def upsert_source_health_batch(
    records: Sequence[Mapping[str, Any]],
    *,
    conn: Any = None,
) -> int:
    """Upsert multiple data source health check records in a single transaction."""
    if not records:
        return 0

    sql = """
        INSERT INTO dalton_data_source_health (
            source_id,
            category,
            status,
            latency_ms,
            latest_date,
            error_type,
            detail,
            checked_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (source_id) DO UPDATE SET
            category    = EXCLUDED.category,
            status      = EXCLUDED.status,
            latency_ms  = EXCLUDED.latency_ms,
            latest_date = EXCLUDED.latest_date,
            error_type  = EXCLUDED.error_type,
            detail      = EXCLUDED.detail,
            checked_at  = NOW()
    """
    count = 0
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                for rec in records:
                    sid = str(rec.get("source_id") or "").strip()
                    if not sid:
                        continue
                    cat = str(rec.get("category") or "").strip()
                    st = _normalize_status(rec.get("status"))
                    lat_raw = rec.get("latency_ms")
                    lat = int(lat_raw) if isinstance(lat_raw, (int, float)) and not isinstance(lat_raw, bool) else None
                    latest = str(rec["latest_date"]).strip()[:64] if rec.get("latest_date") else None
                    err = str(rec["error_type"]).strip()[:128] if rec.get("error_type") else None
                    det = rec.get("detail")
                    det_dict = dict(det) if isinstance(det, Mapping) else {}
                    await cur.execute(
                        sql,
                        (sid, cat, st, lat, latest, err, Jsonb(det_dict)),
                    )
                    count += 1
    return count


async def fetch_all_source_health(
    *,
    conn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fetch all rows from `dalton_data_source_health` keyed by `source_id`."""
    sql = """
        SELECT source_id,
               category,
               status,
               latency_ms,
               latest_date,
               error_type,
               detail,
               checked_at
        FROM dalton_data_source_health
        ORDER BY source_id ASC
    """
    out: dict[str, dict[str, Any]] = {}
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
            for r in rows or []:
                if not isinstance(r, Mapping):
                    continue
                sid = str(r.get("source_id") or "").strip()
                if not sid:
                    continue
                checked_at = r.get("checked_at")
                checked_str = (
                    checked_at.isoformat()
                    if isinstance(checked_at, datetime)
                    else (str(checked_at) if checked_at else None)
                )
                out[sid] = {
                    "source_id": sid,
                    "category": str(r.get("category") or ""),
                    "status": _normalize_status(r.get("status")),
                    "latency_ms": r.get("latency_ms"),
                    "latest_date": r.get("latest_date"),
                    "error_type": r.get("error_type"),
                    "detail": dict(r.get("detail") or {}) if isinstance(r.get("detail"), Mapping) else {},
                    "checked_at": checked_str,
                }
    return out


async def fetch_conversation_archive_stats(
    *,
    top_limit: int = 10,
    conn: Any = None,
) -> dict[str, Any]:
    """Query `dalton_conversation_cards` for active card count, date range, top tickers/topics, and counts by type."""
    summary_sql = """
        SELECT COUNT(*) AS total_cards,
               MIN(date_et)::text AS min_date,
               MAX(date_et)::text AS max_date
        FROM dalton_conversation_cards
        WHERE status = 'active'
    """
    by_type_sql = """
        SELECT type, COUNT(*) AS cnt
        FROM dalton_conversation_cards
        WHERE status = 'active'
        GROUP BY type
    """
    top_subjects_sql = """
        WITH items AS (
            SELECT UPPER(BTRIM(t)) AS label
            FROM dalton_conversation_cards,
                 LATERAL UNNEST(tickers) AS t
            WHERE status = 'active'
              AND BTRIM(t) <> ''
            UNION ALL
            SELECT BTRIM(topic_label) AS label
            FROM dalton_conversation_cards
            WHERE status = 'active'
              AND COALESCE(CARDINALITY(tickers), 0) = 0
              AND BTRIM(topic_label) <> ''
        )
        SELECT label, COUNT(*) AS cnt
        FROM items
        GROUP BY label
        ORDER BY cnt DESC, label ASC
        LIMIT %s
    """
    by_type: dict[str, int] = {t: 0 for t in CARD_TYPES_ORDER}
    top_subjects: list[dict[str, Any]] = []
    total_cards = 0
    min_date: str | None = None
    max_date: str | None = None

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(summary_sql)
            s_row = await cur.fetchone()
            if isinstance(s_row, Mapping):
                total_cards = int(s_row.get("total_cards") or 0)
                min_d = s_row.get("min_date")
                max_d = s_row.get("max_date")
                min_date = str(min_d) if min_d else None
                max_date = str(max_d) if max_d else None

            await cur.execute(by_type_sql)
            t_rows = await cur.fetchall()
            for r in t_rows or []:
                if not isinstance(r, Mapping):
                    continue
                ctype = str(r.get("type") or "").strip()
                if ctype:
                    by_type[ctype] = int(r.get("cnt") or 0)

            await cur.execute(top_subjects_sql, (max(1, int(top_limit)),))
            sub_rows = await cur.fetchall()
            for r in sub_rows or []:
                if not isinstance(r, Mapping):
                    continue
                lbl = str(r.get("label") or "").strip()
                if lbl:
                    top_subjects.append({"label": lbl, "count": int(r.get("cnt") or 0)})

    return {
        "total_cards": total_cards,
        "min_date": min_date,
        "max_date": max_date,
        "top_subjects": top_subjects,
        "by_type": by_type,
    }


def data_map_cache_fingerprint(
    capabilities: Any = None,
) -> str:
    """Compute transport-aware data-map cache fingerprint.

    Keeps ``capabilities_fingerprint()`` unchanged for Standalone Core and
    pre-cutover Everflow, and appends a transport discriminator (``:b3``) only
    when Everflow activates Batch 3 dedicated connectors so pre-cutover and
    post-cutover Everflow data maps never collide in
    ``dalton_data_map_scoped_cache``.
    """
    from src.dalton.capabilities import capabilities_fingerprint, is_batch3_connectors_enabled
    from src.dalton.profile import DeploymentProfile, resolve_profile

    base = capabilities_fingerprint(capabilities)
    if resolve_profile() is not DeploymentProfile.CORE and is_batch3_connectors_enabled():
        return f"{base}:b3"
    return base


def _resolve_cache_fingerprint(fingerprint: str | None) -> str:
    if fingerprint is not None:
        cleaned = str(fingerprint).strip()
        if cleaned:
            return cleaned
    return data_map_cache_fingerprint()


async def get_cached_data_map(
    cache_date: date,
    *,
    capabilities_fingerprint: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch the capability-scoped cached data map row for ``(cache_date, capabilities_fingerprint)``."""
    fp = _resolve_cache_fingerprint(capabilities_fingerprint)
    sql = """
        SELECT cache_date,
               capabilities_fingerprint,
               rendered_markdown,
               payload,
               updated_at
        FROM dalton_data_map_scoped_cache
        WHERE cache_date = %s
          AND capabilities_fingerprint = %s
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (cache_date, fp))
            row = await cur.fetchone()
            if not isinstance(row, Mapping):
                return None
            md = str(row.get("rendered_markdown") or "").strip()
            if not md:
                return None
            updated_at = row.get("updated_at")
            return {
                "cache_date": str(row.get("cache_date") or cache_date.isoformat()),
                "capabilities_fingerprint": str(row.get("capabilities_fingerprint") or fp),
                "rendered_markdown": md,
                "payload": dict(row.get("payload") or {}) if isinstance(row.get("payload"), Mapping) else {},
                "updated_at": (
                    updated_at.isoformat()
                    if isinstance(updated_at, datetime)
                    else (str(updated_at) if updated_at else None)
                ),
            }


async def get_latest_cached_route_metadata(
    *,
    capabilities_fingerprint: str | None = None,
    conn: Any = None,
) -> tuple[list[dict[str, Any]] | None, set[str] | None]:
    """Read the most recent persisted ``catalog_routes`` and ``handoff_routes`` metadata.

    Filters ``dalton_data_map_scoped_cache`` by the active capability fingerprint so
    different profiles/capability sets sharing a database never cross-contaminate
    each other's cached routes. Falls back (read-only) to the legacy
    ``dalton_data_map_cache`` table only in non-Core profiles so a post-``d046``
    Everflow startup backfill preserves dynamic routes without re-probing.
    """
    from src.dalton.profile import is_core_profile

    fp = _resolve_cache_fingerprint(capabilities_fingerprint)
    queries: list[tuple[str, tuple[Any, ...]]] = [
        (
            """
            SELECT payload
            FROM dalton_data_map_scoped_cache
            WHERE capabilities_fingerprint = %s
            ORDER BY cache_date DESC, updated_at DESC
            LIMIT 1
            """,
            (fp,),
        ),
    ]
    if not is_core_profile():
        queries.append(
            (
                """
                SELECT payload
                FROM dalton_data_map_cache
                WHERE (
                    payload->>'capabilities_fingerprint' IS NULL
                    OR payload->>'capabilities_fingerprint' = %s
                )
                ORDER BY cache_date DESC, updated_at DESC
                LIMIT 1
                """,
                (fp,),
            )
        )
    for sql, params in queries:
        try:
            async with get_db_connection(conn) as db_conn:
                async with db_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params)
                    row = await cur.fetchone()
        except Exception:
            continue
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        raw_catalog = payload.get("catalog_routes")
        raw_handoff = payload.get("handoff_routes")
        catalog_routes = (
            [dict(r) for r in raw_catalog if isinstance(r, Mapping)]
            if isinstance(raw_catalog, list)
            else None
        )
        handoff_routes = (
            {str(r).strip() for r in raw_handoff if str(r).strip()}
            if isinstance(raw_handoff, list)
            else None
        )
        if catalog_routes is not None or handoff_routes is not None:
            return catalog_routes, handoff_routes
    return None, None


async def upsert_cached_data_map(
    cache_date: date,
    rendered_markdown: str,
    payload: Mapping[str, Any] | None = None,
    *,
    capabilities_fingerprint: str | None = None,
    conn: Any = None,
) -> None:
    """Write or overwrite the capability-scoped daily cached data map in `dalton_data_map_scoped_cache`."""
    fp = _resolve_cache_fingerprint(capabilities_fingerprint)
    sql = """
        INSERT INTO dalton_data_map_scoped_cache (
            cache_date,
            capabilities_fingerprint,
            rendered_markdown,
            payload,
            updated_at
        )
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (cache_date, capabilities_fingerprint) DO UPDATE SET
            rendered_markdown = EXCLUDED.rendered_markdown,
            payload           = EXCLUDED.payload,
            updated_at        = NOW()
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql,
                    (
                        cache_date,
                        fp,
                        str(rendered_markdown),
                        Jsonb(dict(payload or {})),
                    ),
                )
