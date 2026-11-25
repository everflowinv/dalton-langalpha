"""Dual-profile embedding configuration and model-label to API-model mapping."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.dalton.profile import DeploymentProfile, resolve_profile

#: Matches ``company_wiki_cli.py:54-61``: Google retired ``gemini-embedding-2-preview``
#: in favor of GA ``gemini-embedding-2`` in the same vector space (cosine = 1.0).
#: Stored database labels keep the preview name; upstream API requests map it.
API_MODEL_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "gemini-embedding-2-preview": "gemini-embedding-2",
    }
)

SUPPORTED_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"gemini", "openai"})

DEFAULT_EVERFLOW_PROVIDER = "gemini"
DEFAULT_EVERFLOW_MODEL_LABEL = "gemini-embedding-2-preview"
DEFAULT_EVERFLOW_API_MODEL_NAME = "gemini-embedding-2"
DEFAULT_EVERFLOW_DIMENSIONS = 3072

DEFAULT_CORE_OPENAI_MODEL_LABEL = "text-embedding-3-small"
DEFAULT_CORE_DIMENSIONS = 1536

DEFAULT_TASK_TYPE_DOC = "RETRIEVAL_DOCUMENT"
DEFAULT_TASK_TYPE_QUERY = "RETRIEVAL_QUERY"
SUPPORTED_GEMINI_TASK_TYPES: frozenset[str] = frozenset(
    {
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_QUERY",
        "SEMANTIC_SIMILARITY",
        "CLASSIFICATION",
        "CLUSTERING",
        "QUESTION_ANSWERING",
        "FACT_VERIFICATION",
        "CODE_RETRIEVAL_QUERY",
    }
)
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

#: pgvector ``halfvec`` HNSW indexes support up to 4000 dimensions.
MAX_EMBEDDING_DIMENSIONS = 4000

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


class EmbeddingConfigError(ValueError):
    """Raised when embedding configuration or credentials are invalid or missing."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """Resolved backend embedding configuration."""

    provider: str
    model_label: str
    api_model_name: str
    dimensions: int
    task_type_doc: str = DEFAULT_TASK_TYPE_DOC
    task_type_query: str = DEFAULT_TASK_TYPE_QUERY
    base_url: str = ""


def resolve_api_model_name(model_label: str) -> str:
    """Map stored ``model_label`` to the upstream provider API model name."""
    cleaned = str(model_label or "").strip()
    return API_MODEL_ALIASES.get(cleaned, cleaned)


def validate_embedding_task_type(
    task_type: str | None,
    *,
    provider: str,
    source: str = "task_type",
) -> str:
    """Normalize and validate an embedding ``task_type`` before config use or metadata lock."""
    norm = str(task_type or "").strip().upper()
    if not norm:
        raise EmbeddingConfigError(f"Empty embedding {source}: must be a non-empty task type")
    if str(provider or "").strip().lower() == "gemini" and norm not in SUPPORTED_GEMINI_TASK_TYPES:
        raise EmbeddingConfigError(
            f"Unsupported Gemini embedding {source}={task_type!r}; "
            f"expected one of: {', '.join(sorted(SUPPORTED_GEMINI_TASK_TYPES))}"
        )
    return norm


def is_embed_via_bridge_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when the ``DALTON_EMBED_VIA_BRIDGE`` emergency rollback switch is enabled."""
    env = os.environ if environ is None else environ
    raw = str(env.get("DALTON_EMBED_VIA_BRIDGE") or "").strip().lower()
    return raw in _TRUTHY_VALUES


def _parse_positive_dimensions(raw: str, *, source: str) -> int:
    try:
        dims = int(raw.strip())
    except ValueError as exc:
        raise EmbeddingConfigError(f"Invalid {source}={raw!r}: must be a positive integer") from exc
    if dims < 1 or dims > MAX_EMBEDDING_DIMENSIONS:
        raise EmbeddingConfigError(
            f"Invalid {source}={dims}: must be in range 1..{MAX_EMBEDDING_DIMENSIONS}"
        )
    return dims


def _infer_provider_from_model(model_label: str) -> str | None:
    lowered = model_label.strip().lower()
    if not lowered:
        return None
    if lowered.startswith(("gemini-", "text-embedding-004", "models/gemini", "embedding-001")):
        return "gemini"
    if lowered.startswith(("text-embedding-3", "text-embedding-ada")):
        return "openai"
    return None


def resolve_embedding_config(
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
) -> EmbeddingConfig:
    """Resolve dual-profile embedding settings from env overrides and ``dalton.yaml``.

    - **Everflow**: defaults to ``gemini`` / ``gemini-embedding-2-preview`` (API
      model ``gemini-embedding-2``) / ``3072`` dimensions, matching production.
    - **Core**: defaults to ``1536`` dimensions — ``openai`` /
      ``text-embedding-3-small`` by default (or ``gemini`` /
      ``gemini-embedding-2-preview`` at 1536-d when only a Gemini key is set),
      always overridable via ``DALTON_EMBEDDING_*``.
    """
    env = os.environ if environ is None else environ
    active_profile = (
        resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        if profile is None
        else profile
    )

    env_provider = str(env.get("DALTON_EMBEDDING_PROVIDER") or "").strip().lower()
    env_model = str(env.get("DALTON_EMBEDDING_MODEL") or "").strip()
    env_api_model = str(env.get("DALTON_EMBEDDING_API_MODEL") or "").strip()
    env_dims = str(env.get("DALTON_EMBEDDING_DIMENSIONS") or "").strip()
    env_task_doc = str(env.get("DALTON_EMBEDDING_TASK_TYPE_DOC") or "").strip()
    env_task_query = str(env.get("DALTON_EMBEDDING_TASK_TYPE_QUERY") or "").strip()
    env_base_url = str(
        env.get("DALTON_EMBEDDING_BASE_URL") or env.get("OPENAI_BASE_URL") or ""
    ).strip().rstrip("/")

    if env_provider and env_provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        raise EmbeddingConfigError(
            f"Unsupported DALTON_EMBEDDING_PROVIDER={env_provider!r}; "
            f"expected one of: {', '.join(sorted(SUPPORTED_EMBEDDING_PROVIDERS))}"
        )

    if active_profile is DeploymentProfile.EVERFLOW:
        from src.dalton.settings import get_profile_dalton_config

        conv_cfg = get_profile_dalton_config(DeploymentProfile.EVERFLOW).conversations
        yaml_model = (
            str(conv_cfg.embedding_model_preferred or conv_cfg.embedding_model or "").strip()
            or DEFAULT_EVERFLOW_MODEL_LABEL
        )
        yaml_dims = int(conv_cfg.embedding_dimensions or DEFAULT_EVERFLOW_DIMENSIONS)

        model_label = env_model or yaml_model
        provider = env_provider or _infer_provider_from_model(model_label) or DEFAULT_EVERFLOW_PROVIDER
        dimensions = (
            _parse_positive_dimensions(env_dims, source="DALTON_EMBEDDING_DIMENSIONS")
            if env_dims
            else yaml_dims
        )
    else:
        has_openai_key = bool(str(env.get("OPENAI_API_KEY") or "").strip())
        has_gemini_key = bool(
            str(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()
        )
        if env_provider:
            provider = env_provider
        elif env_model and _infer_provider_from_model(env_model):
            provider = _infer_provider_from_model(env_model) or "openai"
        elif has_gemini_key and not has_openai_key:
            provider = "gemini"
        else:
            provider = "openai"

        if env_model:
            model_label = env_model
        elif provider == "gemini":
            model_label = DEFAULT_EVERFLOW_MODEL_LABEL
        else:
            model_label = DEFAULT_CORE_OPENAI_MODEL_LABEL

        if env_dims:
            dimensions = _parse_positive_dimensions(env_dims, source="DALTON_EMBEDDING_DIMENSIONS")
        else:
            dimensions = DEFAULT_CORE_DIMENSIONS

    if provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        raise EmbeddingConfigError(
            f"Unsupported embedding provider {provider!r}; "
            f"expected one of: {', '.join(sorted(SUPPORTED_EMBEDDING_PROVIDERS))}"
        )

    api_model_name = env_api_model or resolve_api_model_name(model_label)
    if not model_label or not api_model_name:
        raise EmbeddingConfigError("Embedding model_label and api_model_name must be non-empty")

    if dimensions < 1 or dimensions > MAX_EMBEDDING_DIMENSIONS:
        raise EmbeddingConfigError(
            f"Embedding dimensions={dimensions} out of supported range 1..{MAX_EMBEDDING_DIMENSIONS}"
        )

    task_type_doc = validate_embedding_task_type(
        env_task_doc or DEFAULT_TASK_TYPE_DOC,
        provider=provider,
        source="DALTON_EMBEDDING_TASK_TYPE_DOC",
    )
    task_type_query = validate_embedding_task_type(
        env_task_query or DEFAULT_TASK_TYPE_QUERY,
        provider=provider,
        source="DALTON_EMBEDDING_TASK_TYPE_QUERY",
    )
    base_url = env_base_url or (DEFAULT_OPENAI_BASE_URL if provider == "openai" else "")
    return EmbeddingConfig(
        provider=provider,
        model_label=model_label,
        api_model_name=api_model_name,
        dimensions=dimensions,
        task_type_doc=task_type_doc,
        task_type_query=task_type_query,
        base_url=base_url,
    )


def resolve_embedding_api_key(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the provider API key from environment (never logs secret values)."""
    env = os.environ if environ is None else environ
    explicit = str(env.get("DALTON_EMBEDDING_API_KEY") or "").strip()
    if explicit:
        return explicit
    norm = str(provider or "").strip().lower()
    if norm == "gemini":
        return str(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()
    if norm == "openai":
        return str(env.get("OPENAI_API_KEY") or "").strip()
    return ""
