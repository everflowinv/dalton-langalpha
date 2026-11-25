"""Team Model Catalog service package (d051).

Uses lazy attribute loading (PEP 562) so importing lightweight submodules such as
``constants`` from the database layer never creates circular imports.
"""

from __future__ import annotations

import importlib
from typing import Any

from src.dalton.services.team_model_catalog.constants import (
    CATALOG_CHANGED_CHANNEL,
    CATALOG_LOCK_KEY,
    CATALOG_REVISION_HEADER,
    CATALOG_SCHEMA,
    TURN_SNAPSHOT_SCHEMA,
    CatalogConflictError,
    CatalogError,
    CatalogValidationError,
    CatalogVariantCredentialRevokedError,
    CatalogVariantSecurityError,
    catalog_variant_source_tag,
    compute_payload_sha256,
    empty_catalog_payload,
    make_key_fingerprint,
    norm_id,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "CompiledCatalog": ("src.dalton.services.team_model_catalog.compiled", "CompiledCatalog"),
    "cache_compiled_catalog": ("src.dalton.services.team_model_catalog.compiled", "cache_compiled_catalog"),
    "compile_catalog_from_payload": ("src.dalton.services.team_model_catalog.compiled", "compile_catalog_from_payload"),
    "get_compiled_catalog_async": ("src.dalton.services.team_model_catalog.compiled", "get_compiled_catalog_async"),
    "get_compiled_catalog_sync": ("src.dalton.services.team_model_catalog.compiled", "get_compiled_catalog_sync"),
    "invalidate_compiled_catalog_cache": ("src.dalton.services.team_model_catalog.compiled", "invalidate_compiled_catalog_cache"),
    "prewarm_compiled_catalog": ("src.dalton.services.team_model_catalog.compiled", "prewarm_compiled_catalog"),
    "invalidate_catalog_redis_caches": ("src.dalton.services.team_model_catalog.follow", "invalidate_catalog_redis_caches"),
    "publish_catalog_changed": ("src.dalton.services.team_model_catalog.follow", "publish_catalog_changed"),
    "run_catalog_follow_loop": ("src.dalton.services.team_model_catalog.follow", "run_catalog_follow_loop"),
    "start_following": ("src.dalton.services.team_model_catalog.follow", "start_following"),
    "stop_following": ("src.dalton.services.team_model_catalog.follow", "stop_following"),
    "detect_used_legacy_sync": ("src.dalton.services.team_model_catalog.legacy_import", "detect_used_legacy_sync"),
    "extract_disk_legacy_retired_readonly": ("src.dalton.services.team_model_catalog.legacy_import", "extract_disk_legacy_retired_readonly"),
    "resolve_legacy_import": ("src.dalton.services.team_model_catalog.legacy_import", "resolve_legacy_import"),
    "run_startup_legacy_sync_import": ("src.dalton.services.team_model_catalog.legacy_import", "run_startup_legacy_sync_import"),
    "bind_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "bind_catalog_snapshot"),
    "build_effective_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "build_effective_catalog_snapshot"),
    "ensure_run_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "ensure_run_catalog_snapshot"),
    "get_active_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "get_active_catalog_snapshot"),
    "load_run_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "load_run_catalog_snapshot"),
    "lookup_snapshot_modalities": ("src.dalton.services.team_model_catalog.snapshot", "lookup_snapshot_modalities"),
    "lookup_snapshot_notional": ("src.dalton.services.team_model_catalog.snapshot", "lookup_snapshot_notional"),
    "lookup_snapshot_pricing": ("src.dalton.services.team_model_catalog.snapshot", "lookup_snapshot_pricing"),
    "lookup_snapshot_variant_binding": ("src.dalton.services.team_model_catalog.snapshot", "lookup_snapshot_variant_binding"),
    "run_with_catalog_snapshot": ("src.dalton.services.team_model_catalog.snapshot", "run_with_catalog_snapshot"),
    "CatalogVariantHttpx2Transport": ("src.dalton.services.team_model_catalog.transport", "CatalogVariantHttpx2Transport"),
    "CatalogVariantHttpxTransport": ("src.dalton.services.team_model_catalog.transport", "CatalogVariantHttpxTransport"),
    "check_custom_model_against_catalog_variants": ("src.dalton.services.team_model_catalog.transport", "check_custom_model_against_catalog_variants"),
    "enforce_catalog_variant_construction": ("src.dalton.services.team_model_catalog.transport", "enforce_catalog_variant_construction"),
    "get_catalog_variant_bound_urls": ("src.dalton.services.team_model_catalog.transport", "get_catalog_variant_bound_urls"),
    "make_catalog_variant_http_transport": ("src.dalton.services.team_model_catalog.transport", "make_catalog_variant_http_transport"),
    "make_catalog_variant_transport_factory": ("src.dalton.services.team_model_catalog.transport", "make_catalog_variant_transport_factory"),
    "preflight_catalog_variant_url": ("src.dalton.services.team_model_catalog.transport", "preflight_catalog_variant_url"),
    "resolve_catalog_variant_binding_async": ("src.dalton.services.team_model_catalog.transport", "resolve_catalog_variant_binding_async"),
    "resolve_catalog_variant_binding_sync": ("src.dalton.services.team_model_catalog.transport", "resolve_catalog_variant_binding_sync"),
    "url_matches_catalog_variant": ("src.dalton.services.team_model_catalog.transport", "url_matches_catalog_variant"),
    "validate_https_variant_base_url": ("src.dalton.services.team_model_catalog.transport", "validate_https_variant_base_url"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr = target
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr)


__all__ = [
    "CATALOG_CHANGED_CHANNEL",
    "CATALOG_LOCK_KEY",
    "CATALOG_REVISION_HEADER",
    "CATALOG_SCHEMA",
    "TURN_SNAPSHOT_SCHEMA",
    "CatalogConflictError",
    "CatalogError",
    "CatalogValidationError",
    "CatalogVariantCredentialRevokedError",
    "CatalogVariantSecurityError",
    "catalog_variant_source_tag",
    "compute_payload_sha256",
    "empty_catalog_payload",
    "make_key_fingerprint",
    "norm_id",
    *_LAZY_EXPORTS.keys(),
]
