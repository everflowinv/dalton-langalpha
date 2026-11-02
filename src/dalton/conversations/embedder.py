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
import re
import urllib.error
import urllib.request
from typing import Any

from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_embed_url,
    get_dalton_settings,
)

logger = logging.getLogger(__name__)

#: Waits before the 2nd and 3rd ask of one batch. One dropped connection from
#: the bridge to the provider used to fail a whole hour-long archive run
#: (2026-11-02: a single ConnectError surfaced as a bridge 502).
RETRY_DELAYS_S: tuple[float, ...] = (2.0, 5.0)
# The bridge (or what it fronts) briefly unavailable; any other status is an
# answer that asking again would repeat.
_TRANSIENT_STATUSES = frozenset({502, 503, 504})


# The bridge answers 502 for its own missing configuration too; asking again
# cannot fix that, so those reasons are not retried.
_PERMANENT_REASON = re.compile(r"not configured|not found|_missing", re.IGNORECASE)


class BridgeHTTPError(Exception):
    """A non-2xx answer from the bridge, with its ``error`` reason already read."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"HTTP {code}: {reason}" if reason else f"HTTP {code}")
        self.code = code
        self.reason = reason


def _bridge_reason(exc: urllib.error.HTTPError) -> str:
    """The bridge's ``error`` field from an HTTP error body; closes the stream.

    Runs in the worker thread with the request: the body is a socket read.
    """
    try:
        raw = exc.read(2048).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a torn-down stream has no reason to give
        raw = ""
    finally:
        exc.close()
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return ""
    return str(parsed.get("error") or "")[:200] if isinstance(parsed, dict) else ""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, BridgeHTTPError):
        return exc.code in _TRANSIENT_STATUSES and not _PERMANENT_REASON.search(exc.reason)
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


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
    try:
        resp_cm = urllib.request.urlopen(req, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        raise BridgeHTTPError(exc.code, _bridge_reason(exc)) from exc
    with resp_cm as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}


async def _post_embed_with_retry(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout_s: float,
    delays: tuple[float, ...],
) -> dict[str, Any]:
    """One batch's POST, asked again after a transient failure, once per entry in ``delays``."""
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            return await asyncio.to_thread(_post_embed_sync, url, token, payload, timeout_s)
        except Exception as exc:
            reason = exc.reason if isinstance(exc, BridgeHTTPError) else ""
            if delay is None or not _is_transient(exc):
                if reason:
                    raise RuntimeError(f"Host bridge /embed failed: {reason}") from exc
                raise
            logger.warning(
                "bridge /embed attempt %d failed (%s%s); retrying in %.0fs",
                attempt,
                type(exc).__name__,
                f": {reason}" if reason else "",
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def embed_texts_via_bridge(
    texts: list[str],
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
    timeout_s: float = 120.0,
    embed_url: str | None = None,
    bridge_token: str | None = None,
    retry: bool = True,
) -> tuple[list[list[float]], str]:
    """Embed `texts` in batches of up to 100 via the host bridge `/embed` endpoint.

    Returns `(embeddings, model_used)`. ``retry`` covers the batch callers
    (the archive); an interactive caller passes ``False`` so a stalled bridge
    costs it one timeout, not three, before it falls back.
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
        resp = await _post_embed_with_retry(
            url, token, payload, timeout_s, RETRY_DELAYS_S if retry else ()
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
            retry=False,
        )
        return vecs[0] if vecs else None
    except Exception as exc:
        logger.warning("[dalton.conversations.embed] query embed unavailable (%s); falling back to keyword arm", type(exc).__name__)
        return None
