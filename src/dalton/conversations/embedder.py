"""Container-side client for the host `researchdata_bridge` `/embed` endpoint.

Dalton containers never hold `GEMINI_API_KEY`; all embedding calls route over
`host.docker.internal` to `scripts/dalton/researchdata_bridge.py` (`POST /embed`
authenticated via request headers), which delegates to the host embedding
configuration (`gemini-embedding-2-preview` preferred, `gemini-embedding-001`
fallback, 3072 dimensions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any

from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_embed_url,
    get_dalton_settings,
)

logger = logging.getLogger(__name__)


def _post_embed_sync(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    clean_url = url.split("/embed/", 1)[0] + "/embed" if "/embed/" in url else url
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Dalton-Bridge-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(clean_url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}


async def embed_texts_via_bridge(
    texts: list[str],
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
    timeout_s: float = 120.0,
    embed_url: str | None = None,
    bridge_token: str | None = None,
) -> tuple[list[list[float]], str]:
    """Embed `texts` in batches of up to 100 via the host bridge `/embed` endpoint.

    Returns `(embeddings, model_used)`.
    """
    if not texts:
        default_model = get_dalton_settings().conversations.embedding_model_preferred
        return [], default_model

    url = embed_url or get_dalton_bridge_embed_url()
    token = bridge_token if bridge_token is not None else get_dalton_bridge_admin_token()
    model_used = get_dalton_settings().conversations.embedding_model_preferred
    all_vecs: list[list[float]] = []

    batch_size = 100
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        payload = {
            "texts": batch,
            "task_type": task_type,
        }
        resp = await asyncio.to_thread(
            _post_embed_sync,
            url,
            token,
            payload,
            timeout_s,
        )
        if not resp.get("ok") or not isinstance(resp.get("embeddings"), list):
            err = str(resp.get("error") or "bridge_embed_failed")
            raise RuntimeError(f"Host bridge /embed failed: {err}")
        if resp.get("model"):
            model_used = str(resp["model"])
        for vec in resp["embeddings"]:
            all_vecs.append([float(x) for x in vec])

    return all_vecs, model_used


async def embed_query_via_bridge(
    query: str,
    *,
    timeout_s: float = 60.0,
    embed_url: str | None = None,
    bridge_token: str | None = None,
) -> list[float] | None:
    """Embed a single search query via the host bridge (`task_type='RETRIEVAL_QUERY'`)."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        vecs, _ = await embed_texts_via_bridge(
            [q],
            task_type="RETRIEVAL_QUERY",
            timeout_s=timeout_s,
            embed_url=embed_url,
            bridge_token=bridge_token,
        )
        return vecs[0] if vecs else None
    except Exception as exc:
        logger.warning("[dalton.conversations.embed] query embed unavailable (%s); falling back to keyword arm", type(exc).__name__)
        return None
