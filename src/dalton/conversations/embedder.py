"""Conversation archive embedding client (backend direct via ``src.dalton.embeddings``).

Embedding calls run directly in the backend via ``src.dalton.embeddings``
(Gemini / OpenAI, honoring ``dalton_embedding_metadata`` and separating stored
``model_label`` from upstream ``api_model_name``).
"""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any

from src.dalton.embeddings import (
    EmbeddingConfig,
    EmbeddingMetadata,
    EmbeddingMetadataMismatchError,
    embed_query_direct,
    embed_texts_direct,
    ensure_conversation_embedding_schema,
    get_embedding_metadata,
    is_metadata_db_unavailable_error,
    resolve_embedding_config,
    validate_embedding_metadata_match,
    validate_embedding_task_type,
)

logger = logging.getLogger(__name__)


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


async def embed_texts(
    texts: list[str],
    *,
    task_type: str | None = None,
    timeout_s: float = 120.0,
    retry: bool = True,
    conn: Any = None,
) -> tuple[list[list[float]], str]:
    """Embed ``texts`` via the direct backend embedding client."""
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
    """Embed a single search query via the direct backend embedding client."""
    q = (query or "").strip()
    if not q:
        return None

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
