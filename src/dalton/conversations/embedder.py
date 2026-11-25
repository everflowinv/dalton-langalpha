"""Conversation archive embedding client (backend direct default + bridge rollback switch).

By default, embedding calls run directly in the backend via
``src.dalton.embeddings`` (Gemini / OpenAI, honoring ``dalton_embedding_metadata``
and separating stored ``model_label`` from upstream ``api_model_name``).

Setting ``DALTON_EMBED_VIA_BRIDGE=true`` activates emergency rollback to the
legacy host ``researchdata_bridge`` ``POST /embed`` endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from src.dalton.embeddings import (
    DEFAULT_EVERFLOW_PROVIDER,
    DEFAULT_TASK_TYPE_DOC,
    DEFAULT_TASK_TYPE_QUERY,
    EmbeddingConfig,
    EmbeddingMetadata,
    EmbeddingMetadataMismatchError,
    embed_query_direct,
    embed_texts_direct,
    ensure_conversation_embedding_schema,
    get_embedding_metadata,
    is_embed_via_bridge_enabled,
    is_metadata_db_unavailable_error,
    resolve_embedding_config,
    validate_bridge_embedding_match,
    validate_bridge_task_types,
    validate_embedding_metadata_match,
    validate_embedding_task_type,
)
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

    if embed_url:
        url = embed_url
    else:
        from src.dalton.capabilities import CapabilityId, has_capability

        if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
            raise RuntimeError("researchdata_bridge capability is disabled")
        url = get_dalton_bridge_embed_url()
        if not url:
            raise RuntimeError("Host bridge /embed URL is not configured")
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
        if len(resp["embeddings"]) != len(batch):
            raise RuntimeError(
                f"Host bridge /embed returned {len(resp['embeddings'])} vectors for {len(batch)} texts"
            )
        if resp.get("model"):
            model_used = str(resp["model"])
        for vec in resp["embeddings"]:
            all_vecs.append([float(x) for x in vec])

    return all_vecs, model_used


async def _read_locked_metadata_if_available(
    *,
    conn: Any = None,
) -> EmbeddingMetadata | None:
    """Read locked ``dalton_embedding_metadata`` when the DB pool/table is available.

    Only swallows ``UndefinedTable`` or an uninitialized test pool; never swallows
    ``EmbeddingMetadataMismatchError`` or unexpected database errors.
    """
    try:
        return await get_embedding_metadata(conn=conn)
    except Exception as exc:
        if is_metadata_db_unavailable_error(exc):
            logger.debug(
                "[dalton.conversations.embed] metadata lookup skipped (db unavailable: %s)",
                type(exc).__name__,
            )
            return None
        raise


async def _ensure_embedding_schema_if_available(
    cfg: EmbeddingConfig,
    *,
    conn: Any = None,
) -> EmbeddingMetadata | None:
    """Align column/HNSW index and lock ``dalton_embedding_metadata`` when DB is available."""
    try:
        return await ensure_conversation_embedding_schema(cfg, conn=conn)
    except EmbeddingMetadataMismatchError:
        raise
    except Exception as exc:
        if is_metadata_db_unavailable_error(exc):
            logger.debug(
                "[dalton.conversations.embed] schema ensure skipped (db unavailable: %s)",
                type(exc).__name__,
            )
            return None
        raise


async def _ensure_bridge_compatible_before_call(
    *,
    task_type_override: str | None = None,
    conn: Any = None,
) -> EmbeddingMetadata | None:
    """Validate that locked metadata (or effective config when unlocked) matches legacy bridge constraints.

    Rejects bridge usage before sending any HTTP request when provider or doc/query
    task types deviate from the legacy Gemini ``RETRIEVAL_DOCUMENT`` /
    ``RETRIEVAL_QUERY`` contract.
    """
    locked = await _read_locked_metadata_if_available(conn=conn)
    env_doc = str(os.getenv("DALTON_EMBEDDING_TASK_TYPE_DOC") or "").strip().upper()
    env_query = str(os.getenv("DALTON_EMBEDDING_TASK_TYPE_QUERY") or "").strip().upper()

    if locked is not None:
        if locked.provider != DEFAULT_EVERFLOW_PROVIDER:
            raise EmbeddingMetadataMismatchError(
                f"Bridge embedding provider mismatch for scope={locked.scope!r}: "
                f"bridge={DEFAULT_EVERFLOW_PROVIDER!r} != locked={locked.provider!r}"
            )
        validate_bridge_task_types(
            task_type_doc=locked.task_type_doc,
            task_type_query=locked.task_type_query,
            scope=locked.scope,
        )
        eff_doc = (
            str(task_type_override).strip().upper()
            if task_type_override is not None
            else (env_doc or locked.task_type_doc)
        )
        eff_query = env_query or locked.task_type_query
        validate_bridge_task_types(
            task_type_doc=eff_doc,
            task_type_query=eff_query,
            scope=locked.scope,
        )
        return locked

    cfg = resolve_embedding_config()
    eff_doc = (
        str(task_type_override).strip().upper()
        if task_type_override is not None
        else cfg.task_type_doc
    )
    validate_bridge_task_types(
        task_type_doc=eff_doc,
        task_type_query=cfg.task_type_query,
    )
    return None


async def _embed_query_via_bridge_with_model(
    query: str,
    *,
    timeout_s: float = 60.0,
    embed_url: str | None = None,
    bridge_token: str | None = None,
    conn: Any = None,
) -> tuple[list[float] | None, str | None]:
    """Embed a single query via bridge, validating task types, model & dimensions against locked metadata."""
    q = (query or "").strip()
    if not q:
        return None, None
    locked = await _ensure_bridge_compatible_before_call(conn=conn)
    try:
        vecs, model_used = await embed_texts_via_bridge(
            [q],
            task_type=DEFAULT_TASK_TYPE_QUERY,
            timeout_s=timeout_s,
            embed_url=embed_url,
            bridge_token=bridge_token,
            retry=False,
        )
    except EmbeddingMetadataMismatchError:
        raise
    except Exception as exc:
        logger.warning(
            "[dalton.conversations.embed] query embed unavailable (%s); falling back to keyword arm",
            type(exc).__name__,
        )
        return None, None

    vec = vecs[0] if vecs else None
    if vec is not None:
        if locked is None:
            locked = await _read_locked_metadata_if_available(conn=conn)
        if locked is not None:
            validate_bridge_embedding_match(
                model_used,
                len(vec),
                locked,
                task_type_doc=DEFAULT_TASK_TYPE_DOC,
                task_type_query=DEFAULT_TASK_TYPE_QUERY,
            )
    return vec, model_used


async def embed_query_via_bridge(
    query: str,
    *,
    timeout_s: float = 60.0,
    embed_url: str | None = None,
    bridge_token: str | None = None,
    conn: Any = None,
) -> list[float] | None:
    """Embed a single search query via the host bridge (`task_type='RETRIEVAL_QUERY'`)."""
    vec, _ = await _embed_query_via_bridge_with_model(
        query,
        timeout_s=timeout_s,
        embed_url=embed_url,
        bridge_token=bridge_token,
        conn=conn,
    )
    return vec


async def embed_texts(
    texts: list[str],
    *,
    task_type: str | None = None,
    timeout_s: float = 120.0,
    retry: bool = True,
    conn: Any = None,
) -> tuple[list[list[float]], str]:
    """Embed ``texts`` via direct backend client, or via host bridge when ``DALTON_EMBED_VIA_BRIDGE=true``."""
    if is_embed_via_bridge_enabled():
        locked = await _ensure_bridge_compatible_before_call(
            task_type_override=task_type,
            conn=conn,
        )
        vecs, model_used = await embed_texts_via_bridge(
            texts,
            task_type=DEFAULT_TASK_TYPE_DOC,
            timeout_s=timeout_s,
            retry=retry,
        )
        if locked is None:
            locked = await _read_locked_metadata_if_available(conn=conn)
        if locked is not None:
            vec_len = len(vecs[0]) if vecs else None
            validate_bridge_embedding_match(
                model_used,
                vec_len,
                locked,
                task_type_doc=DEFAULT_TASK_TYPE_DOC,
                task_type_query=DEFAULT_TASK_TYPE_QUERY,
            )
        return vecs, model_used

    cfg = resolve_embedding_config()
    eff_task_type = validate_embedding_task_type(
        cfg.task_type_doc if task_type is None else task_type,
        provider=cfg.provider,
        source="task_type",
    )
    effective_cfg = (
        cfg if eff_task_type == cfg.task_type_doc else replace(cfg, task_type_doc=eff_task_type)
    )
    locked = await _read_locked_metadata_if_available(conn=conn)
    if locked is not None:
        validate_embedding_metadata_match(effective_cfg, locked)
    vecs, model_label = await embed_texts_direct(
        texts,
        task_type=eff_task_type,
        config=effective_cfg,
        timeout_s=timeout_s,
        retry=retry,
    )
    if texts:
        await _ensure_embedding_schema_if_available(effective_cfg, conn=conn)
    return vecs, model_label


async def embed_query(
    query: str,
    *,
    timeout_s: float = 60.0,
    conn: Any = None,
) -> list[float] | None:
    """Embed a single search query via direct backend client (or bridge rollback switch)."""
    q = (query or "").strip()
    if not q:
        return None
    if is_embed_via_bridge_enabled():
        return await embed_query_via_bridge(q, timeout_s=timeout_s, conn=conn)

    cfg = resolve_embedding_config()
    locked = await _read_locked_metadata_if_available(conn=conn)
    if locked is not None:
        validate_embedding_metadata_match(cfg, locked)
    try:
        return await embed_query_direct(
            q,
            config=cfg,
            timeout_s=timeout_s,
            retry=False,
        )
    except EmbeddingMetadataMismatchError:
        raise
    except Exception as exc:
        logger.warning(
            "[dalton.conversations.embed] direct query embed unavailable (%s); falling back to keyword arm",
            type(exc).__name__,
        )
        return None
