"""PostgreSQL persistence for OpenClaw conversation archive (`dalton_conversation_cards`, `processed`, `runs`)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

ARCHIVE_ADVISORY_LOCK_KEY = "dalton_conversation_archive"
DEFAULT_ABANDONED_RUN_ERROR_TEXT = (
    "abandoned: previous archive run left status='running' when new run acquired advisory lock"
)


@asynccontextmanager
async def transaction_scope(conn: Any = None) -> AsyncIterator[Any]:
    """Acquire a DB connection (if `conn` is None) and run the body inside a single transaction."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            yield db_conn


async def try_acquire_archive_run_lock(
    conn: Any = None,
    *,
    lock_key: str = ARCHIVE_ADVISORY_LOCK_KEY,
) -> bool:
    """Acquire a Postgres session-level advisory lock for the archive pipeline (`pg_try_advisory_lock(hashtext(%s))`)."""
    sql = "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked"
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (str(lock_key),))
            row = await cur.fetchone()
            if isinstance(row, dict):
                return bool(row.get("locked"))
            if isinstance(row, (tuple, list)) and row:
                return bool(row[0])
            return False


async def release_archive_run_lock(
    conn: Any = None,
    *,
    lock_key: str = ARCHIVE_ADVISORY_LOCK_KEY,
) -> bool:
    """Release the Postgres session-level advisory lock acquired by `try_acquire_archive_run_lock`."""
    sql = "SELECT pg_advisory_unlock(hashtext(%s)) AS unlocked"
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (str(lock_key),))
            row = await cur.fetchone()
            if isinstance(row, dict):
                return bool(row.get("unlocked"))
            if isinstance(row, (tuple, list)) and row:
                return bool(row[0])
            return False


async def mark_stale_running_runs_abandoned(
    *,
    error_text: str = DEFAULT_ABANDONED_RUN_ERROR_TEXT,
    exclude_run_id: str | None = None,
    conn: Any = None,
) -> int:
    """Mark leftover `status = 'running'` rows in `dalton_conversation_runs` as `abandoned` after acquiring the run lock."""
    if exclude_run_id:
        sql = """
            UPDATE dalton_conversation_runs
            SET status       = 'abandoned',
                error_text   = COALESCE(NULLIF(error_text, ''), %s),
                completed_at = COALESCE(completed_at, NOW())
            WHERE status = 'running'
              AND run_id <> %s
        """
        params: tuple[Any, ...] = (str(error_text), str(exclude_run_id))
    else:
        sql = """
            UPDATE dalton_conversation_runs
            SET status       = 'abandoned',
                error_text   = COALESCE(NULLIF(error_text, ''), %s),
                completed_at = COALESCE(completed_at, NOW())
            WHERE status = 'running'
        """
        params = (str(error_text),)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return int(cur.rowcount or 0)

_CARD_SELECT_COLS = """
    card_id, source_type, agent, session_id, session_key, generation, channel, chat_type,
    event_ids, seq_range, ts, date_et, speaker, speaker_role,
    tickers, entities, sectors, topic_label, type, claim, figures,
    quote, quote_event_id, cited_sources, confidence, status, supersedes,
    extractor_model, prompt_version, content_hash, search_text,
    rendered_locations, files_written_at, created_at
"""


def _format_vector_literal(vec: list[float] | None) -> str | None:
    if not vec:
        return None
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def build_card_search_text(card: dict[str, Any]) -> str:
    """Build combined `search_text` column for `pg_trgm` GIN keyword search."""
    parts: list[str] = []
    for key in ("topic_label", "claim", "quote"):
        val = str(card.get(key) or "").strip()
        if val:
            parts.append(val)
    for arr_key in ("tickers", "entities", "sectors"):
        arr = card.get(arr_key) or []
        if isinstance(arr, list) and arr:
            parts.append(" ".join(str(x) for x in arr if x))
    figs = card.get("figures") or []
    if isinstance(figs, list):
        for fig in figs:
            if isinstance(fig, dict):
                f_str = " ".join(
                    str(fig.get(k) or "")
                    for k in ("metric", "value", "unit", "period")
                    if fig.get(k) is not None
                ).strip()
                if f_str:
                    parts.append(f_str)
    sp = card.get("speaker")
    if isinstance(sp, dict) and sp.get("name"):
        parts.append(str(sp["name"]))
    return " | ".join(parts)


async def fetch_processed_event_chunks(
    keys: list[tuple[str, str, str, int]],
    conn: Any = None,
    *,
    out_plans: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> set[tuple[str, str, str, int]]:
    """Return `(agent, session_id, event_id, chunk_idx)` keys already in `dalton_conversation_processed`.

    When `out_plans` is provided, populates `(agent, session_id, event_id) -> {chunk_count, chunk_max_chars, chunk_spans}`
    and returns all processed chunks for the queried events so retries preserve original split boundaries.
    """
    if not keys:
        return set()
    target_set = {
        (str(ag), str(sid), str(eid), int(cidx))
        for ag, sid, eid, cidx in keys
    }
    target_events = {(ag, sid, eid) for ag, sid, eid, _ in target_set}
    agents = sorted({ag for ag, _, _, _ in target_set})
    session_ids = sorted({sid for _, sid, _, _ in target_set})
    event_ids = sorted({eid for _, _, eid, _ in target_set})
    sql = """
        SELECT agent, session_id, event_id, chunk_idx, chunk_count, chunk_max_chars, chunk_spans
        FROM dalton_conversation_processed
        WHERE agent = ANY(%s::text[])
          AND session_id = ANY(%s::text[])
          AND event_id = ANY(%s::text[])
        ORDER BY chunk_idx ASC
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (agents, session_ids, event_ids))
            rows = await cur.fetchall()
            found: set[tuple[str, str, str, int]] = set()
            for r in rows or []:
                ev_key = (str(r["agent"]), str(r["session_id"]), str(r["event_id"]))
                if ev_key not in target_events:
                    continue
                found.add((*ev_key, int(r["chunk_idx"])))
                if out_plans is not None and ev_key not in out_plans:
                    raw_spans = r.get("chunk_spans")
                    out_plans[ev_key] = {
                        "chunk_count": int(r.get("chunk_count") or 1),
                        "chunk_max_chars": int(r.get("chunk_max_chars") or 6000),
                        "chunk_spans": list(raw_spans) if isinstance(raw_spans, list) else [],
                    }
            if out_plans is not None:
                return found
            return found & target_set


async def record_processed_event_chunks(
    rows: list[dict[str, Any]],
    conn: Any = None,
) -> int:
    """Record extracted or discarded `(agent, session_id, event_id, chunk_idx, ...)` in `dalton_conversation_processed`."""
    if not rows:
        return 0
    sql = """
        INSERT INTO dalton_conversation_processed (
            agent, session_id, event_id, chunk_idx,
            chunk_count, chunk_max_chars, chunk_spans,
            status, processed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (agent, session_id, event_id, chunk_idx) DO UPDATE SET
            chunk_count     = EXCLUDED.chunk_count,
            chunk_max_chars = EXCLUDED.chunk_max_chars,
            chunk_spans     = EXCLUDED.chunk_spans,
            status          = EXCLUDED.status,
            processed_at    = NOW()
    """
    count = 0
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                for r in rows:
                    raw_spans = r.get("chunk_spans")
                    spans = list(raw_spans) if isinstance(raw_spans, list) else []
                    await cur.execute(
                        sql,
                        (
                            str(r.get("agent") or "main"),
                            str(r.get("session_id") or ""),
                            str(r.get("event_id") or ""),
                            int(r.get("chunk_idx", 0)),
                            max(1, int(r.get("chunk_count", len(spans) or 1))),
                            max(1, int(r.get("chunk_max_chars", 6000))),
                            Jsonb(spans),
                            str(r.get("status") or "extracted"),
                        ),
                    )
                    count += 1
    return count


async def find_existing_content_hashes(hashes: list[str], conn: Any = None) -> set[str]:
    """Return the subset of `hashes` already present (and not retracted) in `dalton_conversation_cards`."""
    if not hashes:
        return set()
    sql = """
        SELECT content_hash
        FROM dalton_conversation_cards
        WHERE content_hash = ANY(%s)
          AND status != 'retracted'
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (list(hashes),))
            rows = await cur.fetchall()
            return {str(r["content_hash"]) for r in (rows or [])}


async def find_active_cards_for_tickers(
    tickers: list[str], conn: Any = None
) -> list[dict[str, Any]]:
    """Fetch active cards that overlap with `tickers` for `supersedes` detection."""
    norm_tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not norm_tickers:
        return []
    sql = f"""
        SELECT {_CARD_SELECT_COLS}
        FROM dalton_conversation_cards
        WHERE status = 'active'
          AND tickers && %s::text[]
        ORDER BY ts DESC, created_at DESC
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (norm_tickers,))
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or [])]


def _build_card_insert_params(
    card: dict[str, Any],
    embedding: list[float] | None = None,
) -> tuple[Any, ...]:
    vec_literal = _format_vector_literal(embedding)
    speaker_obj = card.get("speaker") if isinstance(card.get("speaker"), dict) else {}
    speaker_role = str(
        card.get("speaker_role") or speaker_obj.get("role") or "participant"
    )
    search_text = str(card.get("search_text") or build_card_search_text(card))
    return (
        str(card["card_id"]),
        str(card["agent"]),
        str(card["session_id"]),
        str(card.get("session_key") or card["session_id"]),
        str(card.get("generation") or ""),
        str(card.get("channel") or ""),
        str(card.get("chat_type") or ""),
        list(card.get("event_ids") or []),
        Jsonb(list(card.get("seq_range") or [])),
        card["ts"],
        card["date_et"],
        Jsonb(speaker_obj),
        speaker_role,
        [str(t).upper() for t in (card.get("tickers") or []) if str(t).strip()],
        [str(e) for e in (card.get("entities") or []) if str(e).strip()],
        [str(s) for s in (card.get("sectors") or []) if str(s).strip()],
        str(card.get("topic_label") or ""),
        str(card["type"]),
        str(card["claim"]),
        Jsonb(list(card.get("figures") or [])),
        str(card["quote"]),
        str(card["quote_event_id"]),
        Jsonb(list(card.get("cited_sources") or [])),
        float(card.get("confidence", 0.8)),
        "active",
        None,
        str(card["extractor_model"]),
        str(card["prompt_version"]),
        str(card["content_hash"]),
        search_text,
        vec_literal,
    )


async def insert_cards_batch(
    cards: list[dict[str, Any]],
    *,
    embeddings: list[list[float] | None] | None = None,
    conn: Any = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Insert a batch of new cards without `supersedes` first, then compute and apply `(ticker, metric, period)`
    `supersedes` updates in the same transaction so no foreign key ever references an uninserted card.

    Returns `(inserted_count, superseded_count, inserted_cards)`.
    """
    if not cards:
        return 0, 0, []

    from src.dalton.conversations.extractor import compute_batch_supersedes

    insert_sql = f"""
        INSERT INTO dalton_conversation_cards (
            card_id, source_type, agent, session_id, session_key, generation, channel, chat_type,
            event_ids, seq_range, ts, date_et, speaker, speaker_role,
            tickers, entities, sectors, topic_label, type, claim, figures,
            quote, quote_event_id, cited_sources, confidence, status, supersedes,
            extractor_model, prompt_version, content_hash, search_text, embedding, files_written_at
        )
        VALUES (
            %s, 'conversation', %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::vector, NULL
        )
        ON CONFLICT (content_hash) DO NOTHING
        RETURNING {_CARD_SELECT_COLS}
    """

    single_tickers = sorted(
        {
            str(t).strip().upper()
            for c in cards
            for t in (c.get("tickers") or [])
            if str(t).strip()
            and len([x for x in (c.get("tickers") or []) if str(x).strip()]) == 1
            and c.get("figures")
        }
    )

    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                existing_active: list[dict[str, Any]] = []
                if single_tickers:
                    await cur.execute(
                        f"""
                        SELECT {_CARD_SELECT_COLS}
                        FROM dalton_conversation_cards
                        WHERE status = 'active'
                          AND tickers && %s::text[]
                        ORDER BY ts DESC, created_at DESC
                        """,
                        (single_tickers,),
                    )
                    existing_rows = await cur.fetchall()
                    existing_active = [dict(r) for r in (existing_rows or [])]

                inserted_cards: list[dict[str, Any]] = []
                for idx, card in enumerate(cards):
                    vec = (
                        embeddings[idx]
                        if embeddings is not None and idx < len(embeddings)
                        else None
                    )
                    params = _build_card_insert_params(card, vec)
                    await cur.execute(insert_sql, params)
                    row = await cur.fetchone()
                    if row is not None:
                        merged = {**card, **dict(row)}
                        merged["status"] = "active"
                        merged["supersedes"] = None
                        inserted_cards.append(merged)

                if not inserted_cards:
                    return 0, 0, []

                superseded_count = compute_batch_supersedes(
                    inserted_cards,
                    existing_active_cards=existing_active,
                )

                superseded_ids = [
                    str(c["card_id"])
                    for c in (*existing_active, *inserted_cards)
                    if c.get("status") == "superseded" and c.get("card_id")
                ]
                if superseded_ids:
                    await cur.execute(
                        """
                        UPDATE dalton_conversation_cards
                        SET status = 'superseded',
                            files_written_at = NULL
                        WHERE card_id = ANY(%s::text[]) AND status = 'active'
                        """,
                        (superseded_ids,),
                    )

                for c in (*existing_active, *inserted_cards):
                    sup_target = c.get("supersedes")
                    if sup_target and c.get("card_id"):
                        await cur.execute(
                            """
                            UPDATE dalton_conversation_cards
                            SET supersedes = COALESCE(supersedes, %s),
                                files_written_at = NULL
                            WHERE card_id = %s
                            """,
                            (str(sup_target), str(c["card_id"])),
                        )

                return len(inserted_cards), superseded_count, inserted_cards


async def insert_card(
    card: dict[str, Any],
    *,
    embedding: list[float] | None = None,
    conn: Any = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Insert a single conversation card idempotently on `content_hash` via `insert_cards_batch`."""
    ins_cnt, _, rows = await insert_cards_batch(
        [card],
        embeddings=[embedding],
        conn=conn,
    )
    if ins_cnt > 0 and rows:
        return True, rows[0]
    return False, None


async def list_cards_for_dates(
    dates_et: list[str | date],
    *,
    active_only: bool = True,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch conversation cards for the given `dates_et` ordered chronologically."""
    if not dates_et:
        return []
    date_strs = [d.isoformat() if isinstance(d, date) else str(d)[:10] for d in dates_et]
    status_clause = "AND status = 'active'" if active_only else ""
    sql = f"""
        SELECT {_CARD_SELECT_COLS}
        FROM dalton_conversation_cards
        WHERE date_et = ANY(%s::date[])
          {status_clause}
        ORDER BY date_et ASC, ts ASC, card_id ASC
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (date_strs,))
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or [])]


def _build_search_filter_clauses(
    *,
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since: str | date | None = None,
    speaker_role: str | None = None,
) -> tuple[str, list[Any]]:
    clauses = ["status = 'active'"]
    params: list[Any] = []

    if tickers:
        norm_t = [str(t).strip().upper() for t in tickers if str(t).strip()]
        if norm_t:
            clauses.append("tickers && %s::text[]")
            params.append(norm_t)

    if types:
        norm_types = [str(tp).strip().lower() for tp in types if str(tp).strip()]
        if norm_types:
            clauses.append("type = ANY(%s::text[])")
            params.append(norm_types)

    if since:
        since_str = since.isoformat() if isinstance(since, date) else str(since)[:10]
        clauses.append("date_et >= %s::date")
        params.append(since_str)

    if speaker_role:
        clauses.append("speaker_role = %s")
        params.append(str(speaker_role).strip().lower())

    return " AND ".join(clauses), params


async def search_cards_dense(
    query_embedding: list[float],
    *,
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since: str | date | None = None,
    speaker_role: str | None = None,
    limit: int = 40,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Vector similarity search arm using pgvector HNSW (`halfvec(3072)` cosine distance)."""
    vec_lit = _format_vector_literal(query_embedding)
    if not vec_lit:
        return []
    where_sql, filter_params = _build_search_filter_clauses(
        tickers=tickers,
        types=types,
        since=since,
        speaker_role=speaker_role,
    )
    sql = f"""
        SELECT {_CARD_SELECT_COLS},
               (1.0 - ((embedding::halfvec(3072)) <=> (%s::vector::halfvec(3072)))) AS dense_score
        FROM dalton_conversation_cards
        WHERE {where_sql}
          AND embedding IS NOT NULL
        ORDER BY (embedding::halfvec(3072)) <=> (%s::vector::halfvec(3072)) ASC
        LIMIT %s
    """
    params = [vec_lit, *filter_params, vec_lit, int(limit)]
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or [])]


async def search_cards_keyword(
    query_text: str,
    *,
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since: str | date | None = None,
    speaker_role: str | None = None,
    limit: int = 40,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Trigram (`pg_trgm`) + substring keyword search arm over `search_text`."""
    q = (query_text or "").strip()
    if not q:
        return []
    where_sql, filter_params = _build_search_filter_clauses(
        tickers=tickers,
        types=types,
        since=since,
        speaker_role=speaker_role,
    )
    terms = [t for t in q.split() if t]
    like_pat = f"%{terms[0]}%" if terms else f"%{q}%"

    sql = f"""
        SELECT {_CARD_SELECT_COLS},
               GREATEST(
                   similarity(search_text, %s),
                   word_similarity(%s, search_text),
                   CASE WHEN search_text ILIKE %s THEN 0.35 ELSE 0.0 END
               ) AS keyword_score
        FROM dalton_conversation_cards
        WHERE {where_sql}
          AND (
              search_text %% %s
              OR %s <%% search_text
              OR search_text ILIKE %s
          )
        ORDER BY keyword_score DESC, date_et DESC, ts DESC
        LIMIT %s
    """
    params = [q, q, like_pat, *filter_params, q, q, like_pat, int(limit)]
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or [])]


async def insert_run_start(
    *,
    run_id: str,
    mode: str,
    extractor_model: str,
    prompt_version: str,
    embedding_model: str | None = None,
    window_start: date | str | None = None,
    window_end: date | str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert or reset a `running` record in `dalton_conversation_runs`."""
    ws = window_start.isoformat() if isinstance(window_start, date) else window_start
    we = window_end.isoformat() if isinstance(window_end, date) else window_end
    sql = """
        INSERT INTO dalton_conversation_runs (
            run_id, mode, window_start, window_end, status,
            extractor_model, embedding_model, prompt_version, started_at
        )
        VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, NOW())
        ON CONFLICT (run_id) DO UPDATE SET
            status = 'running',
            started_at = NOW(),
            completed_at = NULL,
            error_text = NULL
        RETURNING *
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql,
                    (run_id, mode, ws, we, extractor_model, embedding_model, prompt_version),
                )
                row = await cur.fetchone()
                return dict(row) if row else {"run_id": run_id, "status": "running"}


async def update_run_progress(
    run_id: str,
    *,
    embedding_model: str | None = None,
    events_read: int = 0,
    segments_processed: int = 0,
    cards_extracted: int = 0,
    cards_inserted: int = 0,
    cards_deduped: int = 0,
    cards_superseded: int = 0,
    quotes_rejected: int = 0,
    segments_discarded: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    stats: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Update live progress counters on an in-flight `dalton_conversation_runs` row."""
    sql = """
        UPDATE dalton_conversation_runs
        SET embedding_model    = COALESCE(%s, embedding_model),
            events_read        = %s,
            segments_processed = %s,
            cards_extracted    = %s,
            cards_inserted     = %s,
            cards_deduped      = %s,
            cards_superseded   = %s,
            quotes_rejected    = %s,
            segments_discarded = %s,
            input_tokens       = %s,
            output_tokens      = %s,
            total_tokens       = %s,
            stats              = %s
        WHERE run_id = %s
        RETURNING *
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql,
                    (
                        embedding_model,
                        int(events_read),
                        int(segments_processed),
                        int(cards_extracted),
                        int(cards_inserted),
                        int(cards_deduped),
                        int(cards_superseded),
                        int(quotes_rejected),
                        int(segments_discarded),
                        int(input_tokens),
                        int(output_tokens),
                        int(total_tokens),
                        Jsonb(stats or {}),
                        run_id,
                    ),
                )
                row = await cur.fetchone()
                return dict(row) if row else None


async def update_run_finish(
    run_id: str,
    *,
    status: str = "completed",
    embedding_model: str | None = None,
    events_read: int = 0,
    segments_processed: int = 0,
    cards_extracted: int = 0,
    cards_inserted: int = 0,
    cards_deduped: int = 0,
    cards_superseded: int = 0,
    quotes_rejected: int = 0,
    segments_discarded: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    stats: dict[str, Any] | None = None,
    error_text: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Update final statistics and status on `dalton_conversation_runs`."""
    sql = """
        UPDATE dalton_conversation_runs
        SET status             = %s,
            embedding_model    = COALESCE(%s, embedding_model),
            events_read        = %s,
            segments_processed = %s,
            cards_extracted    = %s,
            cards_inserted     = %s,
            cards_deduped      = %s,
            cards_superseded   = %s,
            quotes_rejected    = %s,
            segments_discarded = %s,
            input_tokens       = %s,
            output_tokens      = %s,
            total_tokens       = %s,
            stats              = %s,
            error_text         = %s,
            completed_at       = NOW()
        WHERE run_id = %s
        RETURNING *
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql,
                    (
                        status,
                        embedding_model,
                        int(events_read),
                        int(segments_processed),
                        int(cards_extracted),
                        int(cards_inserted),
                        int(cards_deduped),
                        int(cards_superseded),
                        int(quotes_rejected),
                        int(segments_discarded),
                        int(input_tokens),
                        int(output_tokens),
                        int(total_tokens),
                        Jsonb(stats or {}),
                        error_text,
                        run_id,
                    ),
                )
                row = await cur.fetchone()
                return dict(row) if row else None


async def list_unrendered_dates(conn: Any = None) -> list[str]:
    """Return distinct `date_et` strings that have cards with `files_written_at IS NULL`."""
    sql = """
        SELECT DISTINCT date_et::text AS date_et
        FROM dalton_conversation_cards
        WHERE files_written_at IS NULL
        ORDER BY date_et ASC
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
            return [str(r["date_et"])[:10] for r in (rows or []) if r.get("date_et")]


async def mark_dates_rendered(
    dates_et: list[str | date],
    conn: Any = None,
    *,
    card_locations: dict[str, list[dict[str, Any]]] | None = None,
) -> int:
    """Stamp `files_written_at = NOW()` and persist `rendered_locations` on cards belonging to `dates_et`."""
    if not dates_et:
        return 0
    date_strs = [d.isoformat() if isinstance(d, date) else str(d)[:10] for d in dates_et]
    sql = """
        UPDATE dalton_conversation_cards
        SET files_written_at = NOW(),
            rendered_locations = CASE
                WHEN status = 'active' THEN rendered_locations
                ELSE '[]'::jsonb
            END
        WHERE date_et = ANY(%s::date[])
          AND files_written_at IS NULL
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                if card_locations:
                    for cid, locs in card_locations.items():
                        await cur.execute(
                            """
                            UPDATE dalton_conversation_cards
                            SET rendered_locations = %s,
                                files_written_at = NOW()
                            WHERE card_id = %s
                            """,
                            (Jsonb(list(locs or [])), str(cid)),
                        )
                await cur.execute(sql, (date_strs,))
                return int(cur.rowcount or 0)
