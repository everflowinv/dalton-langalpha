"""Hybrid search (dense vector similarity + trigram keyword + RRF fusion + mild time decay)
for Dalton's OpenClaw conversation archive (`source_type='conversation'`).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from src.dalton.conversations.embedder import embed_query_via_bridge
from src.dalton.database import conversations as db_conv
from src.dalton.database.conversations import build_card_search_text
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)

RRF_K = 60


def format_conversation_locator(card: dict[str, Any]) -> str:
    """Return canonical provenance locator `openclaw://<agent>/<session_id>#<quote_event_id>`."""
    agent = str(card.get("agent") or "main")
    session_id = str(card.get("session_id") or "")
    event_id = str(card.get("quote_event_id") or "")
    return f"openclaw://{agent}/{session_id}#{event_id}"


def compute_recency_decay(
    card_date: str | date | None,
    *,
    reference_date: date | None = None,
    half_life_days: float = 180.0,
    floor: float = 0.7,
) -> float:
    """Compute mild exponential recency multiplier in `[floor, 1.0]`."""
    if not card_date or half_life_days <= 0:
        return 1.0
    ref = reference_date or datetime.now(timezone.utc).date()
    try:
        d = card_date if isinstance(card_date, date) else date.fromisoformat(str(card_date)[:10])
    except ValueError:
        return 1.0
    age_days = max(0, (ref - d).days)
    raw_decay = math.pow(0.5, age_days / float(half_life_days))
    return max(float(floor), min(1.0, raw_decay))


def cosine_similarity(vec_a: list[float] | None, vec_b: list[float] | None) -> float:
    """Compute cosine similarity between two equal-length vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(float(a) * float(a) for a in vec_a))
    norm_b = math.sqrt(sum(float(b) * float(b) for b in vec_b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return dot / (norm_a * norm_b)


def trigram_similarity(query: str, text: str) -> float:
    """Compute character 3-gram Jaccard/Dice similarity + substring boost (for in-memory/Chinese trigram matching)."""
    q = (query or "").strip().lower()
    t = (text or "").strip().lower()
    if not q or not t:
        return 0.0

    def _trigrams(s: str) -> set[str]:
        padded = f"  {s} "
        if len(padded) < 3:
            return {padded}
        return {padded[i : i + 3] for i in range(len(padded) - 2)}

    q_grams = _trigrams(q)
    t_grams = _trigrams(t)
    overlap = len(q_grams & t_grams)
    base_sim = overlap / max(1, len(q_grams))

    # Boost exact token / substring hits (important for 2-char Chinese words & tickers)
    terms = [tok for tok in q.split() if tok]
    if not terms:
        terms = [q]
    term_hits = sum(1 for tok in terms if tok in t)
    sub_boost = 0.35 * (term_hits / len(terms))
    return min(1.0, base_sim + sub_boost)


def rrf_fuse_cards(
    dense_hits: list[dict[str, Any]],
    keyword_hits: list[dict[str, Any]],
    *,
    k: int = RRF_K,
    recency_half_life_days: float = 180.0,
    recency_floor: float = 0.7,
    reference_date: date | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fuse dense vector and trigram keyword ranked lists via Reciprocal Rank Fusion (RRF) with mild recency decay."""
    by_id: dict[str, dict[str, Any]] = {}
    rrf_scores: dict[str, float] = {}
    matched_arms: dict[str, list[str]] = {}
    dense_scores: dict[str, float] = {}
    kw_scores: dict[str, float] = {}

    for rank, item in enumerate(dense_hits, start=1):
        cid = str(item.get("card_id") or "")
        if not cid:
            continue
        by_id[cid] = dict(item)
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
        matched_arms.setdefault(cid, []).append("dense")
        if item.get("dense_score") is not None:
            dense_scores[cid] = float(item["dense_score"])

    for rank, item in enumerate(keyword_hits, start=1):
        cid = str(item.get("card_id") or "")
        if not cid:
            continue
        if cid not in by_id:
            by_id[cid] = dict(item)
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
        if "keyword" not in matched_arms.setdefault(cid, []):
            matched_arms[cid].append("keyword")
        if item.get("keyword_score") is not None:
            kw_scores[cid] = float(item["keyword_score"])

    fused: list[dict[str, Any]] = []
    for cid, card in by_id.items():
        raw_rrf = rrf_scores[cid]
        decay = compute_recency_decay(
            card.get("date_et"),
            reference_date=reference_date,
            half_life_days=recency_half_life_days,
            floor=recency_floor,
        )
        final_score = round(raw_rrf * decay, 6)
        enriched = dict(card)
        enriched.pop("embedding", None)
        enriched["source_type"] = "conversation"
        enriched["source_ref"] = format_conversation_locator(enriched)
        enriched["score"] = final_score
        enriched["rrf_score"] = round(raw_rrf, 6)
        enriched["recency_multiplier"] = round(decay, 4)
        enriched["matched_arms"] = matched_arms.get(cid, [])
        if cid in dense_scores:
            enriched["dense_score"] = round(dense_scores[cid], 4)
        if cid in kw_scores:
            enriched["keyword_score"] = round(kw_scores[cid], 4)
        fused.append(enriched)

    fused.sort(
        key=lambda c: (
            float(c.get("score") or 0.0),
            str(c.get("date_et") or ""),
            str(c.get("ts") or ""),
        ),
        reverse=True,
    )
    return fused[: max(1, int(limit))]


def rank_cards_in_memory(
    cards: list[dict[str, Any]],
    *,
    query: str,
    query_embedding: list[float] | None = None,
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since: str | date | None = None,
    speaker_role: str | None = None,
    limit: int = 10,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    """Run two-arm (dense + trigram keyword) hybrid ranking + RRF fusion over an in-memory card list."""
    norm_tickers = {str(t).strip().upper() for t in (tickers or []) if str(t).strip()}
    norm_types = {str(tp).strip().lower() for tp in (types or []) if str(tp).strip()}
    since_str = (
        since.isoformat()
        if isinstance(since, date)
        else (str(since)[:10] if since else None)
    )
    norm_role = str(speaker_role).strip().lower() if speaker_role else None

    filtered: list[dict[str, Any]] = []
    for c in cards:
        if str(c.get("status") or "active") != "active":
            continue
        if norm_tickers:
            c_tickers = {str(t).upper() for t in (c.get("tickers") or [])}
            if not (norm_tickers & c_tickers):
                continue
        if norm_types and str(c.get("type") or "").lower() not in norm_types:
            continue
        if since_str and str(c.get("date_et") or "")[:10] < since_str:
            continue
        if norm_role:
            c_role = str(
                c.get("speaker_role")
                or (c.get("speaker") or {}).get("role")
                or ""
            ).lower()
            if c_role != norm_role:
                continue
        filtered.append(c)

    dense_hits: list[dict[str, Any]] = []
    if query_embedding:
        scored_dense: list[tuple[float, dict[str, Any]]] = []
        for c in filtered:
            emb = c.get("embedding")
            if isinstance(emb, list) and emb:
                sim = cosine_similarity(query_embedding, emb)
                c_copy = dict(c)
                c_copy["dense_score"] = sim
                scored_dense.append((sim, c_copy))
        scored_dense.sort(key=lambda x: x[0], reverse=True)
        dense_hits = [item for _, item in scored_dense]

    scored_kw: list[tuple[float, dict[str, Any]]] = []
    for c in filtered:
        s_txt = str(c.get("search_text") or build_card_search_text(c))
        kw_sim = trigram_similarity(query, s_txt)
        if kw_sim > 0.0:
            c_copy = dict(c)
            c_copy["keyword_score"] = kw_sim
            scored_kw.append((kw_sim, c_copy))
    scored_kw.sort(key=lambda x: x[0], reverse=True)
    keyword_hits = [item for _, item in scored_kw]

    cfg = get_dalton_settings().conversations
    return rrf_fuse_cards(
        dense_hits,
        keyword_hits,
        recency_half_life_days=cfg.recency_half_life_days,
        recency_floor=cfg.recency_floor,
        reference_date=reference_date,
        limit=limit,
    )


async def hybrid_search_conversations(
    query: str,
    *,
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since: str | date | None = None,
    speaker_role: str | None = None,
    limit: int = 10,
    query_embedding: list[float] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Execute hybrid vector + pg_trgm search over `dalton_conversation_cards` with RRF fusion."""
    eff_limit = max(1, min(50, int(limit or 10)))
    candidate_k = max(eff_limit * 4, 40)

    q_vec = query_embedding
    if q_vec is None:
        q_vec = await embed_query_via_bridge(query)

    dense_hits: list[dict[str, Any]] = []
    if q_vec:
        try:
            dense_hits = await db_conv.search_cards_dense(
                q_vec,
                tickers=tickers,
                types=types,
                since=since,
                speaker_role=speaker_role,
                limit=candidate_k,
                conn=conn,
            )
        except Exception as exc:
            logger.warning("[dalton.conversations.search] dense search arm failed: %s", exc)

    keyword_hits: list[dict[str, Any]] = []
    try:
        keyword_hits = await db_conv.search_cards_keyword(
            query,
            tickers=tickers,
            types=types,
            since=since,
            speaker_role=speaker_role,
            limit=candidate_k,
            conn=conn,
        )
    except Exception as exc:
        logger.warning("[dalton.conversations.search] keyword search arm failed: %s", exc)

    cfg = get_dalton_settings().conversations
    fused = rrf_fuse_cards(
        dense_hits,
        keyword_hits,
        recency_half_life_days=cfg.recency_half_life_days,
        recency_floor=cfg.recency_floor,
        limit=eff_limit,
    )

    declared_sources = [
        {
            "type": "conversation",
            "source_type": "conversation",
            "id": f"conversation:{c['card_id']}",
            "locator": c["source_ref"],
            "title": c.get("topic_label") or c.get("claim") or c["card_id"],
            "date": str(c.get("date_et") or ""),
        }
        for c in fused
    ]

    return {
        "_declared_sources": declared_sources,
        "_sources": declared_sources,
        "ok": True,
        "source_type": "conversation",
        "query": query,
        "filters": {
            "tickers": tickers,
            "types": types,
            "since": str(since) if since else None,
            "speaker_role": speaker_role,
            "limit": eff_limit,
        },
        "dense_arm_active": bool(q_vec),
        "count": len(fused),
        "cards": fused,
    }
