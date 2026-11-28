"""PostgreSQL read-only search persistence for conversation archive (`dalton_conversation_cards`)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

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
    uploader = sp.get("uploaded_by") if isinstance(sp, dict) else None
    if not isinstance(uploader, dict):
        uploader = card.get("uploaded_by")
    if isinstance(uploader, dict) and uploader.get("name"):
        parts.append(str(uploader["name"]))
    return " | ".join(parts)


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
    dimensions: int | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Vector similarity search arm using pgvector HNSW (``halfvec(<dim>)`` cosine distance).

    Supports both legacy Everflow 3072-d and Standalone Core 1536-d indexes by
    reading the locked dimension from ``dalton_embedding_metadata`` (or explicit
    ``dimensions``) and failing fast on any dimension mismatch.
    """
    from src.dalton.embeddings.config import MAX_EMBEDDING_DIMENSIONS
    from src.dalton.embeddings.metadata import (
        EmbeddingDimensionMismatchError,
        get_embedding_metadata,
        is_metadata_db_unavailable_error,
    )

    vec_lit = _format_vector_literal(query_embedding)
    if not vec_lit:
        return []
    q_dim = len(query_embedding)
    if q_dim < 1 or q_dim > MAX_EMBEDDING_DIMENSIONS:
        raise EmbeddingDimensionMismatchError(
            f"Query embedding dimension {q_dim} out of supported range 1..{MAX_EMBEDDING_DIMENSIONS}"
        )

    where_sql, filter_params = _build_search_filter_clauses(
        tickers=tickers,
        types=types,
        since=since,
        speaker_role=speaker_role,
    )
    async with get_db_connection(conn) as db_conn:
        if dimensions is not None:
            if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not (1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS):
                raise EmbeddingDimensionMismatchError(
                    f"Invalid explicit dimensions={dimensions!r}; expected int in 1..{MAX_EMBEDDING_DIMENSIONS}"
                )
            eff_dim = dimensions
        else:
            locked_meta = None
            try:
                locked_meta = await get_embedding_metadata(conn=db_conn)
            except Exception as exc:
                if is_metadata_db_unavailable_error(exc):
                    logger.debug(
                        "[dalton.database.conversations] dalton_embedding_metadata lookup skipped (%s)",
                        type(exc).__name__,
                    )
                else:
                    raise
            eff_dim = locked_meta.dimensions if locked_meta is not None else q_dim

        if q_dim != eff_dim:
            raise EmbeddingDimensionMismatchError(
                f"Query embedding has {q_dim} dimensions, but conversation archive is locked to {eff_dim}"
            )

        sql = f"""
            SELECT {_CARD_SELECT_COLS},
                   (1.0 - ((embedding::halfvec({eff_dim})) <=> (%s::vector::halfvec({eff_dim})))) AS dense_score
            FROM dalton_conversation_cards
            WHERE {where_sql}
              AND embedding IS NOT NULL
            ORDER BY (embedding::halfvec({eff_dim})) <=> (%s::vector::halfvec({eff_dim})) ASC
            LIMIT %s
        """
        params = [vec_lit, *filter_params, vec_lit, int(limit)]
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
