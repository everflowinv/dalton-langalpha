"""Raw psycopg3 SQL queries for Dalton provider status and telemetry events (`dalton_provider_events`, P0N.8)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from src.dalton.settings import get_dalton_broker_token
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{8,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+|"
    r"ya29\.[A-Za-z0-9_\-.]+|Bearer\s+[A-Za-z0-9_\-.]+|"
    r"(?:refresh_token|access_token|api_key|client_secret)\s*[:=]\s*['\"]?[^\s,'\"]+)",
    re.IGNORECASE,
)

ERROR_EVENT_KINDS = ("probe_fail", "refresh_fail", "call_fail", "journal_fail")


def sanitize_error_text(text: str | BaseException | None, max_len: int = 200) -> str:
    """Strip credentials/tokens and truncate error text before storing or returning."""
    if text is None:
        return ""
    raw = f"{text.__class__.__name__}: {text}" if isinstance(text, BaseException) else str(text)
    broker_tok = get_dalton_broker_token()
    if broker_tok and broker_tok in raw:
        raw = raw.replace(broker_tok, "***")
    scrubbed = _SECRET_PATTERN.sub("[REDACTED]", raw)
    return scrubbed[:max_len]


def sanitize_event_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Recursively scrub credentials from event detail dictionary."""
    if not isinstance(detail, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in detail.items():
        key_str = str(k)
        if any(
            secret_word in key_str.lower()
            for secret_word in ("token", "secret", "password", "api_key", "authorization")
        ):
            continue
        if isinstance(v, str):
            out[key_str] = sanitize_error_text(v, max_len=200)
        elif isinstance(v, (int, float, bool)) or v is None:
            out[key_str] = v
        elif isinstance(v, dict):
            out[key_str] = sanitize_event_detail(v)
        else:
            out[key_str] = sanitize_error_text(str(v), max_len=200)
    return out


async def record_provider_event(
    provider: str,
    kind: str,
    *,
    user_id: str | None = None,
    model_key: str | None = None,
    detail: dict[str, Any] | None = None,
    conn: Any = None,
    best_effort: bool = True,
) -> int | None:
    """Insert a sanitized event into `dalton_provider_events`."""
    safe_detail = sanitize_event_detail(detail)
    payload_json = json.dumps(safe_detail, ensure_ascii=False)

    async def _do_insert(c: Any) -> int | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_provider_events (user_id, provider, model_key, kind, detail)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING event_id
                """,
                (user_id, provider, model_key, kind, payload_json),
            )
            row = await cur.fetchone()
            return int(row["event_id"]) if row and row.get("event_id") is not None else None

    try:
        if conn is not None:
            return await _do_insert(conn)
        async with get_db_connection() as db_conn:
            return await _do_insert(db_conn)
    except Exception as exc:
        if not best_effort:
            raise
        logger.debug("[DaltonProviderEvents] failed to record %s/%s: %s", provider, kind, exc)
        return None


insert_provider_event = record_provider_event
log_provider_event = record_provider_event


async def list_recent_provider_events(
    provider: str | None = None,
    *,
    user_id: str | None = None,
    limit: int = 10,
    since: datetime | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch recent provider events ordered by `created_at DESC`."""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if provider is not None:
        clauses.append("provider = %s")
        params.append(provider)
    if user_id is not None:
        clauses.append("(user_id = %s OR user_id IS NULL)")
        params.append(user_id)
    if since is not None:
        clauses.append("created_at >= %s")
        params.append(since)
    if kinds:
        clauses.append("kind = ANY(%s)")
        params.append(list(kinds))

    params.append(max(1, int(limit)))
    where_sql = " AND ".join(clauses)

    async def _do_select(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT event_id, user_id, provider, model_key, kind, detail, created_at
                FROM dalton_provider_events
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [dict(r) for r in await cur.fetchall()]

    try:
        if conn is not None:
            return await _do_select(conn)
        async with get_db_connection() as db_conn:
            return await _do_select(db_conn)
    except Exception as exc:
        logger.debug("[DaltonProviderEvents] list_recent_provider_events failed: %s", exc)
        return []


fetch_recent_provider_events = list_recent_provider_events
get_recent_provider_events = list_recent_provider_events


async def count_provider_errors_24h(
    provider: str | None = None,
    *,
    user_id: str | None = None,
    since: datetime | None = None,
    conn: Any = None,
) -> dict[str, int]:
    """Return error event counts per provider over the last 24 hours."""
    cutoff = since or (datetime.now(timezone.utc) - timedelta(hours=24))
    clauses = ["created_at >= %s", "kind = ANY(%s)"]
    params: list[Any] = [cutoff, list(ERROR_EVENT_KINDS)]
    if provider is not None:
        clauses.append("provider = %s")
        params.append(provider)
    if user_id is not None:
        clauses.append("(user_id = %s OR user_id IS NULL)")
        params.append(user_id)

    where_sql = " AND ".join(clauses)

    async def _do_count(c: Any) -> dict[str, int]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT provider, COUNT(*) AS cnt
                FROM dalton_provider_events
                WHERE {where_sql}
                GROUP BY provider
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return {str(r["provider"]): int(r["cnt"] or 0) for r in rows}

    try:
        if conn is not None:
            return await _do_count(conn)
        async with get_db_connection() as db_conn:
            return await _do_count(db_conn)
    except Exception as exc:
        logger.debug("[DaltonProviderEvents] count_provider_errors_24h failed: %s", exc)
        return {}


async def get_last_refresh_event(
    provider: str,
    *,
    user_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return the latest OAuth token refresh event (`refresh_ok` or `refresh_fail`) for `provider`."""
    rows = await list_recent_provider_events(
        provider=provider,
        user_id=user_id,
        limit=1,
        kinds=["refresh_ok", "refresh_fail"],
        conn=conn,
    )
    return rows[0] if rows else None
