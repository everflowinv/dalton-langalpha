"""Backend embedding package for Dalton (dual-profile, metadata-locked, Gemini & OpenAI).

Replaces container-to-host ``researchdata_bridge /embed`` hops with direct
backend embedding calls while preserving production vector parity:

- Separates stored ``model_label`` (e.g. ``gemini-embedding-2-preview``) from
  the upstream ``api_model_name`` (``gemini-embedding-2``).
- Preserves per-text ``Content(parts=[Part(text=t)])`` batching for Gemini so
  ``gemini-embedding-2`` never folds a list of plain strings into one vector.
- Locks active provider/model/dimensions in ``dalton_embedding_metadata``
  (migration ``d052``) and supports both Everflow 3072-d and Standalone Core
  1536-d pgvector ``halfvec`` HNSW indexes.
- Supports emergency rollback via ``DALTON_EMBED_VIA_BRIDGE=true``.
"""

from src.dalton.embeddings.client import (
    DEFAULT_MAX_DIRECT_CONCURRENCY,
    GEMINI_RATE_LIMIT_DELAYS_S,
    GEMINI_TRANSIENT_DELAYS_S,
    MAX_BATCH_SIZE,
    MAX_RETRY_AFTER_S,
    OPENAI_RATE_LIMIT_DELAYS_S,
    OPENAI_TRANSIENT_DELAYS_S,
    EmbeddingError,
    EmbeddingProviderHTTPError,
    EmbeddingResponseError,
    embed_query_direct,
    embed_texts_direct,
    resolve_max_direct_concurrency,
)
from src.dalton.embeddings.config import (
    API_MODEL_ALIASES,
    DEFAULT_CORE_DIMENSIONS,
    DEFAULT_CORE_OPENAI_MODEL_LABEL,
    DEFAULT_EVERFLOW_API_MODEL_NAME,
    DEFAULT_EVERFLOW_DIMENSIONS,
    DEFAULT_EVERFLOW_MODEL_LABEL,
    DEFAULT_EVERFLOW_PROVIDER,
    DEFAULT_TASK_TYPE_DOC,
    DEFAULT_TASK_TYPE_QUERY,
    MAX_EMBEDDING_DIMENSIONS,
    SUPPORTED_EMBEDDING_PROVIDERS,
    SUPPORTED_GEMINI_TASK_TYPES,
    EmbeddingConfig,
    EmbeddingConfigError,
    is_embed_via_bridge_enabled,
    resolve_api_model_name,
    resolve_embedding_api_key,
    resolve_embedding_config,
    validate_embedding_task_type,
)
from src.dalton.embeddings.metadata import (
    DALTON_EMBEDDING_METADATA_DDL,
    DEFAULT_EMBEDDING_SCOPE,
    EVERFLOW_DEFAULT_METADATA,
    EmbeddingDimensionMismatchError,
    EmbeddingMetadata,
    EmbeddingMetadataMismatchError,
    build_alter_embedding_column_ddl,
    build_halfvec_index_ddl,
    ensure_conversation_embedding_schema,
    get_embedding_metadata,
    is_metadata_db_unavailable_error,
    parse_vector_dimensions,
    resolve_locked_embedding_dimensions,
    upsert_embedding_metadata,
    validate_bridge_embedding_match,
    validate_bridge_task_types,
    validate_embedding_metadata_match,
)

__all__ = [
    "API_MODEL_ALIASES",
    "DALTON_EMBEDDING_METADATA_DDL",
    "DEFAULT_CORE_DIMENSIONS",
    "DEFAULT_CORE_OPENAI_MODEL_LABEL",
    "DEFAULT_EMBEDDING_SCOPE",
    "DEFAULT_EVERFLOW_API_MODEL_NAME",
    "DEFAULT_EVERFLOW_DIMENSIONS",
    "DEFAULT_EVERFLOW_MODEL_LABEL",
    "DEFAULT_EVERFLOW_PROVIDER",
    "DEFAULT_MAX_DIRECT_CONCURRENCY",
    "DEFAULT_TASK_TYPE_DOC",
    "DEFAULT_TASK_TYPE_QUERY",
    "EVERFLOW_DEFAULT_METADATA",
    "GEMINI_RATE_LIMIT_DELAYS_S",
    "GEMINI_TRANSIENT_DELAYS_S",
    "MAX_BATCH_SIZE",
    "MAX_EMBEDDING_DIMENSIONS",
    "MAX_RETRY_AFTER_S",
    "OPENAI_RATE_LIMIT_DELAYS_S",
    "OPENAI_TRANSIENT_DELAYS_S",
    "SUPPORTED_EMBEDDING_PROVIDERS",
    "SUPPORTED_GEMINI_TASK_TYPES",
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "EmbeddingDimensionMismatchError",
    "EmbeddingError",
    "EmbeddingMetadata",
    "EmbeddingMetadataMismatchError",
    "EmbeddingProviderHTTPError",
    "EmbeddingResponseError",
    "build_alter_embedding_column_ddl",
    "build_halfvec_index_ddl",
    "embed_query_direct",
    "embed_texts_direct",
    "ensure_conversation_embedding_schema",
    "get_embedding_metadata",
    "is_embed_via_bridge_enabled",
    "is_metadata_db_unavailable_error",
    "parse_vector_dimensions",
    "resolve_api_model_name",
    "resolve_embedding_api_key",
    "resolve_embedding_config",
    "resolve_locked_embedding_dimensions",
    "resolve_max_direct_concurrency",
    "upsert_embedding_metadata",
    "validate_bridge_embedding_match",
    "validate_bridge_task_types",
    "validate_embedding_metadata_match",
    "validate_embedding_task_type",
]
