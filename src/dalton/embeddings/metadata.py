"""DDL, metadata lock validation, and dimension management for ``dalton_embedding_metadata``."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.dalton.embeddings.config import (
    DEFAULT_EVERFLOW_API_MODEL_NAME,
    DEFAULT_EVERFLOW_DIMENSIONS,
    DEFAULT_EVERFLOW_MODEL_LABEL,
    DEFAULT_EVERFLOW_PROVIDER,
    DEFAULT_TASK_TYPE_DOC,
    DEFAULT_TASK_TYPE_QUERY,
    MAX_EMBEDDING_DIMENSIONS,
    EmbeddingConfig,
    validate_embedding_task_type,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_SCOPE = "conversation_archive"

#: Canonical DDL mirrored by migration ``d052_embedding_metadata_and_dimension.py``.
DALTON_EMBEDDING_METADATA_DDL = """
CREATE TABLE IF NOT EXISTS dalton_embedding_metadata (
    scope           TEXT PRIMARY KEY CHECK (btrim(scope) <> ''),
    provider        TEXT NOT NULL CHECK (btrim(provider) <> ''),
    model_label     TEXT NOT NULL CHECK (btrim(model_label) <> ''),
    api_model_name  TEXT NOT NULL CHECK (btrim(api_model_name) <> ''),
    dimensions      INTEGER NOT NULL CHECK (dimensions > 0 AND dimensions <= 4000),
    task_type_doc   TEXT NOT NULL DEFAULT 'RETRIEVAL_DOCUMENT'
                    CHECK (btrim(task_type_doc) <> ''),
    task_type_query TEXT NOT NULL DEFAULT 'RETRIEVAL_QUERY'
                    CHECK (btrim(task_type_query) <> ''),
    locked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""".strip()

_VECTOR_DIMS_RE = re.compile(r"^vector\((\d+)\)$", re.IGNORECASE)


class EmbeddingMetadataMismatchError(RuntimeError):
    """Raised when runtime embedding config deviates from locked ``dalton_embedding_metadata``."""


class EmbeddingDimensionMismatchError(EmbeddingMetadataMismatchError):
    """Raised when vector or column dimensions do not match the locked metadata dimension."""


@dataclass(frozen=True)
class EmbeddingMetadata:
    """Row representation of ``dalton_embedding_metadata``."""

    scope: str
    provider: str
    model_label: str
    api_model_name: str
    dimensions: int
    task_type_doc: str = DEFAULT_TASK_TYPE_DOC
    task_type_query: str = DEFAULT_TASK_TYPE_QUERY

    @classmethod
    def from_config(
        cls,
        config: EmbeddingConfig,
        *,
        scope: str = DEFAULT_EMBEDDING_SCOPE,
    ) -> EmbeddingMetadata:
        return cls(
            scope=scope,
            provider=config.provider,
            model_label=config.model_label,
            api_model_name=config.api_model_name,
            dimensions=config.dimensions,
            task_type_doc=config.task_type_doc,
            task_type_query=config.task_type_query,
        )


EVERFLOW_DEFAULT_METADATA = EmbeddingMetadata(
    scope=DEFAULT_EMBEDDING_SCOPE,
    provider=DEFAULT_EVERFLOW_PROVIDER,
    model_label=DEFAULT_EVERFLOW_MODEL_LABEL,
    api_model_name=DEFAULT_EVERFLOW_API_MODEL_NAME,
    dimensions=DEFAULT_EVERFLOW_DIMENSIONS,
    task_type_doc=DEFAULT_TASK_TYPE_DOC,
    task_type_query=DEFAULT_TASK_TYPE_QUERY,
)


def _validate_dimensions_int(dimensions: int) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise EmbeddingDimensionMismatchError(f"dimensions must be an int, got {dimensions!r}")
    if dimensions < 1 or dimensions > MAX_EMBEDDING_DIMENSIONS:
        raise EmbeddingDimensionMismatchError(
            f"dimensions={dimensions} out of valid range 1..{MAX_EMBEDDING_DIMENSIONS}"
        )
    return dimensions


def parse_vector_dimensions(pg_type: str | None) -> int | None:
    """Parse PostgreSQL ``format_type(atttypid, atttypmod)`` such as ``vector(1536)``."""
    if not pg_type:
        return None
    m = _VECTOR_DIMS_RE.match(str(pg_type).strip())
    if not m:
        return None
    return int(m.group(1))


def build_alter_embedding_column_ddl(dimensions: int) -> str:
    """Return validated DDL to alter ``dalton_conversation_cards.embedding`` to ``vector(<dim>)``."""
    dim = _validate_dimensions_int(dimensions)
    return f"ALTER TABLE dalton_conversation_cards ALTER COLUMN embedding TYPE vector({dim})"


def build_halfvec_index_ddl(dimensions: int) -> str:
    """Return validated DDL for the ``halfvec(<dim>)`` HNSW cosine index on conversation cards."""
    dim = _validate_dimensions_int(dimensions)
    return (
        "CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_embedding_hnsw "
        "ON dalton_conversation_cards "
        f"USING hnsw ((embedding::halfvec({dim})) halfvec_cosine_ops)"
    )


def is_metadata_db_unavailable_error(exc: BaseException) -> bool:
    """Return True only when DB metadata lookup cannot run because the pool is closed or table is unmigrated."""
    if isinstance(exc, EmbeddingMetadataMismatchError):
        return False
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return True
    if isinstance(exc, RuntimeError) and "database pool is not open" in str(exc).lower():
        return True
    return False


def validate_embedding_metadata_match(
    config: EmbeddingConfig,
    locked: EmbeddingMetadata,
) -> None:
    """Raise immediately when runtime embedding config deviates from locked metadata."""
    if config.dimensions != locked.dimensions:
        raise EmbeddingDimensionMismatchError(
            f"Embedding dimension mismatch for scope={locked.scope!r}: "
            f"configured dimensions={config.dimensions}, locked dimensions={locked.dimensions} "
            f"(provider={locked.provider!r}, model_label={locked.model_label!r}). "
            "Align DALTON_EMBEDDING_DIMENSIONS with the locked index or reindex from backup."
        )
    mismatches: list[str] = []
    if config.provider != locked.provider:
        mismatches.append(f"provider configured={config.provider!r} != locked={locked.provider!r}")
    if config.model_label != locked.model_label:
        mismatches.append(
            f"model_label configured={config.model_label!r} != locked={locked.model_label!r}"
        )
    if config.api_model_name != locked.api_model_name:
        mismatches.append(
            f"api_model_name configured={config.api_model_name!r} != locked={locked.api_model_name!r}"
        )
    if config.task_type_doc != locked.task_type_doc:
        mismatches.append(
            f"task_type_doc configured={config.task_type_doc!r} != locked={locked.task_type_doc!r}"
        )
    if config.task_type_query != locked.task_type_query:
        mismatches.append(
            f"task_type_query configured={config.task_type_query!r} != locked={locked.task_type_query!r}"
        )
    if mismatches:
        raise EmbeddingMetadataMismatchError(
            f"Embedding configuration mismatch for scope={locked.scope!r}: "
            + "; ".join(mismatches)
        )


def _row_to_metadata(row: dict[str, Any]) -> EmbeddingMetadata:
    return EmbeddingMetadata(
        scope=str(row["scope"]),
        provider=str(row["provider"]),
        model_label=str(row["model_label"]),
        api_model_name=str(row["api_model_name"]),
        dimensions=int(row["dimensions"]),
        task_type_doc=str(row.get("task_type_doc") or DEFAULT_TASK_TYPE_DOC),
        task_type_query=str(row.get("task_type_query") or DEFAULT_TASK_TYPE_QUERY),
    )


async def get_embedding_metadata(
    scope: str = DEFAULT_EMBEDDING_SCOPE,
    *,
    conn: Any = None,
) -> EmbeddingMetadata | None:
    """Fetch the locked ``dalton_embedding_metadata`` row for ``scope``, or ``None`` if absent."""
    sql = """
        SELECT scope, provider, model_label, api_model_name,
               dimensions, task_type_doc, task_type_query
        FROM dalton_embedding_metadata
        WHERE scope = %s
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (str(scope),))
            row = await cur.fetchone()
            if not row:
                return None
            return _row_to_metadata(dict(row))


async def upsert_embedding_metadata(
    meta: EmbeddingMetadata,
    *,
    conn: Any = None,
) -> EmbeddingMetadata:
    """Lock ``meta`` into ``dalton_embedding_metadata`` if unlocked; verify exact match if already locked."""
    _validate_dimensions_int(meta.dimensions)
    norm_task_doc = validate_embedding_task_type(
        meta.task_type_doc,
        provider=meta.provider,
        source="task_type_doc",
    )
    norm_task_query = validate_embedding_task_type(
        meta.task_type_query,
        provider=meta.provider,
        source="task_type_query",
    )
    insert_sql = """
        INSERT INTO dalton_embedding_metadata (
            scope, provider, model_label, api_model_name,
            dimensions, task_type_doc, task_type_query
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (scope) DO NOTHING
    """
    select_sql = """
        SELECT scope, provider, model_label, api_model_name,
               dimensions, task_type_doc, task_type_query
        FROM dalton_embedding_metadata
        WHERE scope = %s
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    insert_sql,
                    (
                        meta.scope,
                        meta.provider,
                        meta.model_label,
                        meta.api_model_name,
                        int(meta.dimensions),
                        norm_task_doc,
                        norm_task_query,
                    ),
                )
                await cur.execute(select_sql, (meta.scope,))
                row = await cur.fetchone()
                if not row:
                    raise RuntimeError(
                        f"Failed to read back dalton_embedding_metadata for scope={meta.scope!r}"
                    )
                stored = _row_to_metadata(dict(row))
    cfg = EmbeddingConfig(
        provider=meta.provider,
        model_label=meta.model_label,
        api_model_name=meta.api_model_name,
        dimensions=meta.dimensions,
        task_type_doc=norm_task_doc,
        task_type_query=norm_task_query,
    )
    validate_embedding_metadata_match(cfg, stored)
    return stored


async def ensure_conversation_embedding_schema(
    config: EmbeddingConfig,
    *,
    scope: str = DEFAULT_EMBEDDING_SCOPE,
    conn: Any = None,
) -> EmbeddingMetadata:
    """Ensure ``dalton_conversation_cards.embedding`` and HNSW index match ``config`` and lock metadata.

    - If ``dalton_embedding_metadata`` already has a locked row for ``scope``,
      validates ``config`` against it and raises on any deviation.
    - If unlocked (e.g. a fresh Standalone Core install using 1536-d), inspects
      ``dalton_conversation_cards``:
      * Refuses to alter the column if non-null embeddings already exist with a
        different dimension.
      * When 0 embedded rows exist and the column dimension differs from
        ``config.dimensions``, alters ``embedding`` to ``vector(<dim>)`` and
        rebuilds ``ix_dalton_conv_cards_embedding_hnsw`` on ``halfvec(<dim>)``.
      * Locks the metadata row inside the same transaction.
    """
    _validate_dimensions_int(config.dimensions)
    norm_task_doc = validate_embedding_task_type(
        config.task_type_doc,
        provider=config.provider,
        source="task_type_doc",
    )
    norm_task_query = validate_embedding_task_type(
        config.task_type_query,
        provider=config.provider,
        source="task_type_query",
    )
    config = replace(config, task_type_doc=norm_task_doc, task_type_query=norm_task_query)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_embedding_metadata:{scope}",),
                )
                await cur.execute(
                    """
                    SELECT scope, provider, model_label, api_model_name,
                           dimensions, task_type_doc, task_type_query
                    FROM dalton_embedding_metadata
                    WHERE scope = %s
                    FOR UPDATE
                    """,
                    (str(scope),),
                )
                existing_row = await cur.fetchone()
                if existing_row:
                    locked = _row_to_metadata(dict(existing_row))
                    validate_embedding_metadata_match(config, locked)
                    return locked

                await cur.execute(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS col_type
                    FROM pg_attribute a
                    WHERE a.attrelid = 'public.dalton_conversation_cards'::regclass
                      AND a.attname = 'embedding'
                      AND NOT a.attisdropped
                    """
                )
                col_row = await cur.fetchone()
                current_dim = parse_vector_dimensions(
                    col_row["col_type"] if isinstance(col_row, dict) else None
                )

                await cur.execute(
                    "SELECT count(*) AS cnt FROM dalton_conversation_cards WHERE embedding IS NOT NULL"
                )
                cnt_row = await cur.fetchone()
                non_null_count = int(cnt_row["cnt"]) if isinstance(cnt_row, dict) else 0

                if current_dim is not None and current_dim != config.dimensions:
                    if non_null_count > 0:
                        raise EmbeddingDimensionMismatchError(
                            f"Cannot switch dalton_conversation_cards.embedding from "
                            f"vector({current_dim}) to vector({config.dimensions}) while "
                            f"{non_null_count} embedded rows already exist."
                        )
                    await cur.execute("DROP INDEX IF EXISTS ix_dalton_conv_cards_embedding_hnsw")
                    await cur.execute(build_alter_embedding_column_ddl(config.dimensions))
                    await cur.execute(build_halfvec_index_ddl(config.dimensions))

                await cur.execute(
                    """
                    INSERT INTO dalton_embedding_metadata (
                        scope, provider, model_label, api_model_name,
                        dimensions, task_type_doc, task_type_query
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (scope) DO NOTHING
                    """,
                    (
                        str(scope),
                        config.provider,
                        config.model_label,
                        config.api_model_name,
                        int(config.dimensions),
                        config.task_type_doc,
                        config.task_type_query,
                    ),
                )
                await cur.execute(
                    """
                    SELECT scope, provider, model_label, api_model_name,
                           dimensions, task_type_doc, task_type_query
                    FROM dalton_embedding_metadata
                    WHERE scope = %s
                    """,
                    (str(scope),),
                )
                locked_row = await cur.fetchone()
                if not locked_row:
                    raise RuntimeError(
                        f"Failed to lock dalton_embedding_metadata for scope={scope!r}"
                    )
                locked = _row_to_metadata(dict(locked_row))
                validate_embedding_metadata_match(config, locked)
                return locked


async def resolve_locked_embedding_dimensions(
    scope: str = DEFAULT_EMBEDDING_SCOPE,
    *,
    conn: Any = None,
    fallback: int | None = None,
) -> int:
    """Return locked dimensions from ``dalton_embedding_metadata``, falling back safely if unlocked."""
    try:
        meta = await get_embedding_metadata(scope, conn=conn)
        if meta is not None:
            return meta.dimensions
    except Exception as exc:
        if not is_metadata_db_unavailable_error(exc):
            raise
        logger.debug(
            "[dalton.embeddings.metadata] unable to read dalton_embedding_metadata (%s); using fallback",
            type(exc).__name__,
        )
    if fallback is not None:
        return _validate_dimensions_int(fallback)
    from src.dalton.embeddings.config import resolve_embedding_config

    return resolve_embedding_config().dimensions
