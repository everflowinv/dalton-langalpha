"""Internal library probe: checks for new AlphaEngine / GuidePoint / Transcript / Sales Notes docs (PK.16)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal

logger = logging.getLogger(__name__)


async def _query_db_internal_docs(
    *,
    ticker: str,
    workspace_id: Optional[str] = None,
    since_ts: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Query internal research documents from DB if available (best-effort, zero-token)."""
    try:
        from psycopg.rows import dict_row
        from src.server.database.pool import get_db_connection

        sym = (ticker or "").strip().upper()
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if since_ts:
                    await cur.execute(
                        """
                        SELECT doc_id, source, doc_type, title, snippet, published_at
                        FROM dalton_internal_documents
                        WHERE ticker = %s AND published_at > %s
                        ORDER BY published_at DESC
                        LIMIT 25
                        """,
                        (sym, since_ts),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT doc_id, source, doc_type, title, snippet, published_at
                        FROM dalton_internal_documents
                        WHERE ticker = %s
                        ORDER BY published_at DESC
                        LIMIT 25
                        """,
                        (sym,),
                    )
                rows = await cur.fetchall()
                return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.debug("[DALTON_INTERNAL_PROBE] DB query skipped for %s: %s", ticker, e)
        return []


async def probe_internal(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    workspace_id: Optional[str] = None,
    docs_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Detect new internal research documents newer than `last_internal_doc_id` / `last_internal_doc_ts`."""
    state = dict(watch_state or {})
    docs: Optional[list[dict[str, Any]]] = docs_override
    if docs is None:
        pending = state.get("pending_internal_docs")
        if isinstance(pending, list) and pending:
            docs = pending
        elif isinstance(state.get("internal_docs_feed"), list):
            docs = state.get("internal_docs_feed")
        elif workspace_id:
            docs = await _query_db_internal_docs(
                ticker=ticker,
                workspace_id=workspace_id,
                since_ts=state.get("last_internal_doc_ts"),
            )

    if not isinstance(docs, list) or not docs:
        return [], state

    seen_ids = set(state.get("seen_internal_doc_ids") or [])
    if state.get("last_internal_doc_id"):
        seen_ids.add(str(state["last_internal_doc_id"]))

    signals: list[ProbeSignal] = []
    newest_ts: Optional[str] = state.get("last_internal_doc_ts")
    newest_id: Optional[str] = state.get("last_internal_doc_id")

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("doc_id") or doc.get("id") or "").strip()
        if not doc_id or doc_id in seen_ids:
            continue
        source = str(doc.get("source") or "internal")
        doc_type = str(doc.get("doc_type") or doc.get("type") or source)
        title = str(doc.get("title") or doc_id)
        snippet = str(doc.get("snippet") or doc.get("summary") or doc.get("abstract") or "")[:500]
        pub_at = doc.get("published_at") or doc.get("created_at")
        pub_str = str(pub_at) if pub_at is not None else None

        signals.append(
            ProbeSignal(
                probe="internal",
                signal_key=f"doc:{source}:{doc_id}",
                priority="P1",
                summary=f"{ticker} 新增内部研究文献 [{source}]：{title}",
                payload={
                    "source": source,
                    "doc_type": doc_type,
                    "doc_id": doc_id,
                    "title": title,
                    "snippet": snippet,
                    "published_at": pub_str,
                },
            )
        )
        seen_ids.add(doc_id)
        newest_id = doc_id
        if pub_str and (newest_ts is None or pub_str > str(newest_ts)):
            newest_ts = pub_str

    state["seen_internal_doc_ids"] = sorted(seen_ids)[-200:]
    if newest_id:
        state["last_internal_doc_id"] = newest_id
    if newest_ts:
        state["last_internal_doc_ts"] = newest_ts
    if "pending_internal_docs" in state:
        state["pending_internal_docs"] = []
    return signals, state
