"""Internal library probe: checks for new AlphaEngine / GuidePoint / Transcript / Sales Notes docs (PK.16)."""

from __future__ import annotations

from typing import Any, Optional

from src.dalton.autonomy.probes import ProbeSignal


async def probe_internal(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    docs_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Detect new internal research documents newer than `last_internal_doc_id` / `last_internal_doc_ts`."""
    state = dict(watch_state or {})
    docs = docs_override if docs_override is not None else state.get("pending_internal_docs")
    if not isinstance(docs, list) or not docs:
        return [], state

    seen_ids = set(state.get("seen_internal_doc_ids") or [])
    signals: list[ProbeSignal] = []

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("doc_id") or doc.get("id") or "").strip()
        if not doc_id or doc_id in seen_ids:
            continue
        source = str(doc.get("source") or "internal")
        title = str(doc.get("title") or doc_id)
        signals.append(
            ProbeSignal(
                probe="internal",
                signal_key=f"doc:{source}:{doc_id}",
                priority="P1",
                summary=f"{ticker} 新增内部研究文献 [{source}]：{title}",
                payload={"source": source, "doc_id": doc_id, "title": title, "published_at": doc.get("published_at")},
            )
        )
        seen_ids.add(doc_id)

    state["seen_internal_doc_ids"] = sorted(seen_ids)[-200:]
    if "pending_internal_docs" in state:
        state["pending_internal_docs"] = []
    return signals, state
