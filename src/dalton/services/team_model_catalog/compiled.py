"""Three-layer ``CompiledCatalog`` builder and revision-keyed read-only worker cache.

PostgreSQL (``dalton_team_model_catalog``) is the sole source of truth for the
active revision. Each worker keeps a bounded ``OrderedDict`` (capacity 8) of
immutable ``CompiledCatalog`` instances keyed by
``(revision, overlay_stamp, oauth_sync_enabled, legacy_retired_fingerprint)``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
    COMPILED_CATALOG_CACHE_MAXSIZE,
    compute_payload_sha256,
    empty_catalog_payload,
    norm_id,
    normalize_cash_pricing_card,
)

logger = logging.getLogger(__name__)

_MANIFEST_DIR = Path(__file__).resolve().parents[3] / "llms" / "manifest"
_MODELS_JSON_PATH = _MANIFEST_DIR / "models.json"
_PROVIDERS_JSON_PATH = _MANIFEST_DIR / "providers.json"
_TASK_COSTS_JSON_PATH = _MANIFEST_DIR / "task_costs.json"

_CACHE_LOCK = threading.RLock()
_COMPILED_BY_REVISION: OrderedDict[tuple[int, str, bool, str, bool], "CompiledCatalog"] = OrderedDict()
_HIGHEST_PREWARMED_REVISION: int = -1

# Narrow routing/connection keys locked on PINNED_OVERLAY_MODEL_KEYS (e.g. grok-4.7-oauth).
_PINNED_ROUTE_TOP_KEYS = ("provider", "model_id", "system_provider")
_PINNED_CONNECTION_PARAM_KEYS = (
    "base_url",
    "api_base",
    "openai_api_base",
    "anthropic_api_url",
    "api_endpoint",
    "default_headers",
    "additional_headers",
    "extra_headers",
)


@dataclass(frozen=True)
class CompiledCatalog:
    """Immutable compiled snapshot of the 3-layer system model catalog for revision ``R``."""

    revision: int
    payload_sha256: str
    overlay_stamp: str
    oauth_sync_enabled: bool
    legacy_sync_import_status: str
    legacy_retired_fingerprint: str
    model_config: Any  # ModelConfig
    dalton_meta: dict[str, dict[str, Any]]
    notional_price_index: dict[str, Any]
    task_costs: Any  # TaskCosts | None
    antigravity_series: list[dict[str, Any]]
    provider_variants: dict[str, dict[str, Any]]
    variant_bound_urls: dict[str, str]
    factory_retired_keys: frozenset[str]
    readonly_legacy_retired_keys: frozenset[str]
    team_retired_keys: frozenset[str]
    curated_setup: list[str] | None
    payload: dict[str, Any] = field(default_factory=dict)
    factory_model_keys: frozenset[str] = field(default_factory=frozenset)
    from_db: bool = True


def deep_merge_patch_with_null_delete(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    """Recursively merge ``patch`` into ``target``, deleting keys explicitly set to ``None``."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, Mapping):
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
                target[key] = existing
            deep_merge_patch_with_null_delete(existing, value)
        else:
            target[key] = copy.deepcopy(value)


def _compute_retired_fingerprint(
    status: str,
    last_known_legacy_retired: list[str] | tuple[str, ...] | set[str],
    extra_readonly: set[str] | frozenset[str] | None = None,
) -> str:
    combined = sorted(
        {str(k).strip() for k in (last_known_legacy_retired or []) if str(k).strip()}
        | {str(k).strip() for k in (extra_readonly or ()) if str(k).strip()}
    )
    raw = f"{status}|" + ",".join(combined)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_json_dict(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _is_oauth_key(value: str) -> bool:
    from src.dalton.llm.overlay import _is_oauth_model_key

    return _is_oauth_model_key(str(value or "").strip().lower())


def _filter_payload_for_oauth_disabled(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip all ``*-oauth`` models, providers, and ``dalton_meta`` from a DB payload."""
    from src.dalton.llm.overlay import _filter_generic_overlay

    filtered = _filter_generic_overlay(dict(payload))
    # Carry non-overlay payload sections while stripping oauth keys
    raw_meta = payload.get("dalton_meta")
    if isinstance(raw_meta, Mapping):
        filtered["dalton_meta"] = {
            str(k): copy.deepcopy(v)
            for k, v in raw_meta.items()
            if not _is_oauth_key(str(k)) and isinstance(v, Mapping)
        }
    else:
        filtered["dalton_meta"] = {}

    raw_retired = payload.get("retired")
    if isinstance(raw_retired, list):
        filtered["retired"] = [
            copy.deepcopy(item)
            for item in raw_retired
            if isinstance(item, Mapping) and not _is_oauth_key(str(item.get("key") or ""))
        ]
    else:
        filtered["retired"] = []

    raw_tc = payload.get("task_costs")
    if isinstance(raw_tc, Mapping):
        tc_models = {
            str(k): copy.deepcopy(v)
            for k, v in (raw_tc.get("models") or {}).items()
            if not _is_oauth_key(str(k))
        }
        tc_bounds = {
            str(k): copy.deepcopy(v)
            for k, v in (raw_tc.get("bounds") or {}).items()
            if not _is_oauth_key(str(k))
        }
        filtered["task_costs"] = {"models": tc_models, "bounds": tc_bounds}
    else:
        filtered["task_costs"] = {}

    raw_series = payload.get("series_overrides")
    filtered["series_overrides"] = copy.deepcopy(raw_series) if isinstance(raw_series, Mapping) else {}
    raw_curated = payload.get("curated_setup")
    if isinstance(raw_curated, list):
        filtered["curated_setup"] = [
            str(k).strip() for k in raw_curated if str(k).strip() and not _is_oauth_key(str(k))
        ]
    else:
        filtered["curated_setup"] = None
    return filtered


def _merge_db_payload_layer(
    db_payload: Mapping[str, Any],
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
    dalton_meta: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Apply Layer 3 DB payload onto Layers 1–2 with upgrade same-name promotion.

    Returns ``{variant_slug: flattened_or_raw_entry}`` for catalog-defined provider variants.
    """
    from src.dalton.llm.overlay import _deep_merge_patch

    catalog_variants: dict[str, dict[str, Any]] = {}

    # 1. Provider variants (marked catalog_defined=True, own_api_key=True)
    providers_block = db_payload.get("providers") or {}
    if isinstance(providers_block, Mapping):
        target_prov_cfg = manifest.setdefault("provider_config", {})
        prov_variants = providers_block.get("provider_variants") or {}
        if isinstance(prov_variants, Mapping):
            for group_name, variants_map in prov_variants.items():
                if not isinstance(variants_map, Mapping):
                    continue
                group_obj = target_prov_cfg.get(group_name)
                if not isinstance(group_obj, dict):
                    logger.warning(
                        "Team catalog provider_variants references unknown group %r; skipping",
                        group_name,
                    )
                    continue
                target_variants = group_obj.setdefault("variants", {})
                for v_name, v_entry in variants_map.items():
                    if not isinstance(v_entry, Mapping):
                        continue
                    enriched = copy.deepcopy(dict(v_entry))
                    enriched["catalog_defined"] = True
                    enriched["own_api_key"] = True
                    enriched.pop("env_key", None)
                    enriched.pop("api_key", None)
                    enriched.pop("models_url", None)
                    enriched.pop("platform", None)
                    target_variants[str(v_name)] = enriched
                    catalog_variants[str(v_name)] = {
                        **enriched,
                        "group": str(group_name),
                        "parent_provider": str(enriched.get("parent_provider") or group_name),
                    }

        # 2. Pricing cards (with upgrade same-name promotion via norm_id)
        prov_models = providers_block.get("models") or {}
        if isinstance(prov_models, Mapping):
            target_models = manifest.setdefault("models", {})
            for p_name, p_list in prov_models.items():
                if not isinstance(p_list, list):
                    continue
                normalized_list: list[Any] = []
                for raw_item in p_list:
                    if isinstance(raw_item, Mapping):
                        c_item = copy.deepcopy(dict(raw_item))
                        if isinstance(c_item.get("pricing"), Mapping):
                            c_item["pricing"] = normalize_cash_pricing_card(c_item["pricing"])
                        normalized_list.append(c_item)
                    else:
                        normalized_list.append(copy.deepcopy(raw_item))
                existing_cards = target_models.get(p_name)
                if not isinstance(existing_cards, list):
                    target_models[p_name] = normalized_list
                    continue
                by_norm: dict[str, dict[str, Any]] = {}
                for card in existing_cards:
                    if isinstance(card, dict) and card.get("id"):
                        by_norm[norm_id(str(card["id"]))] = card
                for item in normalized_list:
                    if not isinstance(item, Mapping):
                        continue
                    item_id = str(item.get("id") or "").strip()
                    n_id = norm_id(item_id)
                    if n_id and n_id in by_norm:
                        # §7.2 Upgrade same-name promotion: convert DB card into deep patch
                        logger.info(
                            "Team catalog pricing card %s.%s promoted to override patch over factory baseline",
                            p_name,
                            item_id,
                        )
                        _deep_merge_patch(by_norm[n_id], dict(item))
                        if isinstance(by_norm[n_id].get("pricing"), Mapping):
                            by_norm[n_id]["pricing"] = normalize_cash_pricing_card(
                                by_norm[n_id]["pricing"]
                            )
                    else:
                        cloned = copy.deepcopy(dict(item))
                        existing_cards.append(cloned)
                        if n_id:
                            by_norm[n_id] = cloned

        # 3. Pricing card overrides
        prov_model_overrides = providers_block.get("model_overrides") or {}
        if isinstance(prov_model_overrides, Mapping):
            target_models = manifest.setdefault("models", {})
            for p_name, patches_by_id in prov_model_overrides.items():
                if not isinstance(patches_by_id, Mapping):
                    continue
                cards = target_models.get(p_name)
                if not isinstance(cards, list):
                    logger.info(
                        "Team catalog providers.model_overrides references unknown provider %r; skipping",
                        p_name,
                    )
                    continue
                by_exact = {
                    str(c.get("id")): c for c in cards if isinstance(c, dict) and c.get("id")
                }
                by_norm = {
                    norm_id(str(c.get("id"))): c for c in cards if isinstance(c, dict) and c.get("id")
                }
                for card_id, patch in patches_by_id.items():
                    if not isinstance(patch, Mapping):
                        continue
                    card = by_exact.get(str(card_id)) or by_norm.get(norm_id(str(card_id)))
                    if card is None:
                        logger.info(
                            "Team catalog providers.model_overrides orphan card %s.%s; skipping",
                            p_name,
                            card_id,
                        )
                        continue
                    patch_copy = copy.deepcopy(dict(patch))
                    clear_pricing = "pricing" in patch_copy and patch_copy["pricing"] is None
                    if clear_pricing:
                        patch_copy.pop("pricing", None)
                    elif isinstance(patch_copy.get("pricing"), Mapping):
                        raw_p_patch = dict(patch_copy["pricing"])
                        if "cache_read" in raw_p_patch:
                            cr_val = raw_p_patch.pop("cache_read")
                            if raw_p_patch.get("cached_input") is None and cr_val is not None:
                                raw_p_patch["cached_input"] = cr_val
                        patch_copy["pricing"] = raw_p_patch
                    deep_merge_patch_with_null_delete(card, patch_copy)
                    if clear_pricing:
                        card.pop("pricing", None)
                    elif isinstance(card.get("pricing"), Mapping):
                        card["pricing"] = normalize_cash_pricing_card(card["pricing"])

    # 4. Models (with §7.2 upgrade same-name promotion by key.lower())
    key_lower_map = {str(k).lower(): str(k) for k in llm_config}
    raw_models = db_payload.get("models") or {}
    if isinstance(raw_models, Mapping):
        for key, entry in raw_models.items():
            if not isinstance(entry, Mapping):
                continue
            key_str = str(key)
            existing_key = key_lower_map.get(key_str.lower())
            if existing_key is not None and isinstance(llm_config.get(existing_key), dict):
                logger.info(
                    "Team catalog model %r promoted to deep override patch over factory key %r",
                    key_str,
                    existing_key,
                )
                deep_merge_patch_with_null_delete(llm_config[existing_key], dict(entry))
                if llm_config[existing_key].get("parameters") == {}:
                    llm_config[existing_key].pop("parameters", None)
            else:
                cleaned_entry = copy.deepcopy(dict(entry))
                for k_null in [k for k, v in cleaned_entry.items() if v is None]:
                    cleaned_entry.pop(k_null, None)
                if isinstance(cleaned_entry.get("parameters"), dict):
                    for pk in [k for k, v in cleaned_entry["parameters"].items() if v is None]:
                        cleaned_entry["parameters"].pop(pk, None)
                    if not cleaned_entry["parameters"]:
                        cleaned_entry.pop("parameters", None)
                if isinstance(cleaned_entry.get("reasoning"), dict):
                    for rk in [k for k, v in cleaned_entry["reasoning"].items() if v is None]:
                        cleaned_entry["reasoning"].pop(rk, None)
                llm_config[key_str] = cleaned_entry
                key_lower_map[key_str.lower()] = key_str

    # 5. Model overrides (skip orphan overrides with INFO log per §7.3; explicit null deletes keys)
    raw_overrides = db_payload.get("model_overrides") or {}
    if isinstance(raw_overrides, Mapping):
        for key, patch in raw_overrides.items():
            if not isinstance(patch, Mapping):
                continue
            key_str = str(key)
            existing_key = key_str if key_str in llm_config else key_lower_map.get(key_str.lower())
            if existing_key is None or not isinstance(llm_config.get(existing_key), dict):
                logger.info(
                    "Team catalog orphan model_overrides entry %r skipped (missing from factory baseline)",
                    key_str,
                )
                continue
            deep_merge_patch_with_null_delete(llm_config[existing_key], dict(patch))
            if llm_config[existing_key].get("parameters") == {}:
                llm_config[existing_key].pop("parameters", None)

    # 6. dalton_meta deep merge with explicit null deletion (§2.2 #1)
    raw_meta = db_payload.get("dalton_meta") or {}
    if isinstance(raw_meta, Mapping):
        for key, patch in raw_meta.items():
            if not isinstance(patch, Mapping):
                continue
            key_str = str(key)
            if key_str not in dalton_meta:
                dalton_meta[key_str] = {}
            deep_merge_patch_with_null_delete(dalton_meta[key_str], patch)

    return catalog_variants


def _enforce_narrow_pinned_routes(
    llm_config: dict[str, Any],
    native_overlay: Mapping[str, Any] | None,
) -> None:
    """Lock only low-level route & connection keys on ``PINNED_OVERLAY_MODEL_KEYS``.

    Preserves admin overrides to ``display_name``, ``visible``, ratings
    (``intelligence``, ``speed``, ``plain_language``), ``reasoning`` default, etc.
    """
    from src.dalton.llm.overlay import PINNED_OVERLAY_MODEL_KEYS

    native_models = (native_overlay or {}).get("models") or {}
    if not isinstance(native_models, Mapping):
        return
    for key in PINNED_OVERLAY_MODEL_KEYS:
        factory_entry = native_models.get(key)
        if not isinstance(factory_entry, Mapping):
            continue
        current = llm_config.get(key)
        if not isinstance(current, dict):
            llm_config[key] = copy.deepcopy(dict(factory_entry))
            continue
        for route_key in _PINNED_ROUTE_TOP_KEYS:
            if route_key in factory_entry:
                current[route_key] = copy.deepcopy(factory_entry[route_key])
            else:
                current.pop(route_key, None)
        factory_params = factory_entry.get("parameters")
        if isinstance(factory_params, Mapping):
            cur_params = current.setdefault("parameters", {})
            if isinstance(cur_params, dict):
                for pkey in _PINNED_CONNECTION_PARAM_KEYS:
                    if pkey in factory_params:
                        cur_params[pkey] = copy.deepcopy(factory_params[pkey])


def _merge_task_costs(db_payload: Mapping[str, Any]) -> Any:
    """Merge shipped ``task_costs.json`` with Layer 3 DB ``task_costs`` overrides."""
    from src.llms.task_costs import parse_task_costs

    base_data = _load_json_dict(_TASK_COSTS_JSON_PATH) if _TASK_COSTS_JSON_PATH.is_file() else {}
    if not base_data:
        return None
    merged = copy.deepcopy(base_data)
    db_tc = db_payload.get("task_costs")
    if isinstance(db_tc, Mapping):
        db_models = db_tc.get("models")
        if isinstance(db_models, Mapping):
            target_models = merged.setdefault("models", {})
            for model_key, figures in db_models.items():
                if isinstance(figures, Mapping):
                    target_models[str(model_key)] = copy.deepcopy(dict(figures))
        db_bounds = db_tc.get("bounds")
        if isinstance(db_bounds, Mapping):
            target_bounds = merged.setdefault("bounds", {})
            for model_key, bound in db_bounds.items():
                if bound is None:
                    target_bounds.pop(str(model_key), None)
                elif isinstance(bound, Mapping):
                    existing_target = target_bounds.get(str(model_key))
                    if isinstance(existing_target, dict):
                        deep_merge_patch_with_null_delete(existing_target, bound)
                    else:
                        target_bounds[str(model_key)] = copy.deepcopy(dict(bound))
    return parse_task_costs(merged)


def compile_catalog_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    revision: int = 0,
    payload_sha256: str | None = None,
    oauth_sync_enabled: bool | None = None,
    legacy_sync_import_status: str = "not_applicable",
    last_known_legacy_retired: list[str] | tuple[str, ...] | set[str] | None = None,
    extra_readonly_legacy_retired: set[str] | frozenset[str] | None = None,
    native_overlay_override: Mapping[str, Any] | None = None,
    upstream_models_override: Mapping[str, Any] | None = None,
    upstream_providers_override: Mapping[str, Any] | None = None,
    from_db: bool = True,
) -> CompiledCatalog:
    """Compile the complete 3-layer ``CompiledCatalog`` for ``payload`` at ``revision``."""
    from src.dalton.llm.antigravity_series import build_antigravity_series_catalog
    from src.dalton.llm.notional_pricing import build_notional_price_index_for_catalog
    from src.dalton.llm.overlay import (
        NATIVE_OVERLAY_PATH,
        _drop_retired_models,
        _filter_generic_overlay,
        _is_native_oauth_sync_enabled,
        _merge_single_overlay,
        current_overlay_stamp,
        inherit_antigravity_gemini_ratings,
        load_overlay_data,
    )
    from src.llms.llm import ModelConfig

    oauth_on = (
        bool(oauth_sync_enabled)
        if oauth_sync_enabled is not None
        else _is_native_oauth_sync_enabled()
    )
    normalized_payload = (
        copy.deepcopy(dict(payload))
        if isinstance(payload, Mapping)
        else empty_catalog_payload()
    )
    normalized_payload.setdefault("schema", CATALOG_SCHEMA)
    sha = payload_sha256 or compute_payload_sha256(normalized_payload)

    # Layer 1: Upstream manifests
    llm_config: dict[str, Any] = (
        copy.deepcopy(dict(upstream_models_override))
        if upstream_models_override is not None
        else _load_json_dict(_MODELS_JSON_PATH)
    )
    manifest: dict[str, Any] = (
        copy.deepcopy(dict(upstream_providers_override))
        if upstream_providers_override is not None
        else _load_json_dict(_PROVIDERS_JSON_PATH)
    )

    # Layer 2: Checked-in static native_overlay.json
    native_overlay = (
        copy.deepcopy(dict(native_overlay_override))
        if native_overlay_override is not None
        else (load_overlay_data(NATIVE_OVERLAY_PATH) or {})
    )
    dalton_meta: dict[str, dict[str, Any]] = {}
    factory_retired_keys: set[str] = set()
    if isinstance(native_overlay, Mapping) and native_overlay:
        native_to_merge = (
            dict(native_overlay) if oauth_on else _filter_generic_overlay(dict(native_overlay))
        )
        _merge_single_overlay(native_to_merge, "native", llm_config, manifest)
        for rec in native_overlay.get("retired") or []:
            if isinstance(rec, Mapping) and rec.get("key"):
                factory_retired_keys.add(str(rec["key"]).strip())
        raw_native_meta = native_overlay.get("dalton_meta") or {}
        if isinstance(raw_native_meta, Mapping):
            for k, v in raw_native_meta.items():
                if not oauth_on and _is_oauth_key(str(k)):
                    continue
                if isinstance(v, Mapping):
                    dalton_meta[str(k)] = copy.deepcopy(dict(v))

    # Track factory-available keys BEFORE Layer 3 (needed for failed-import degraded admission §2.6)
    factory_available_keys = frozenset(
        str(k) for k in llm_config if str(k) not in factory_retired_keys
    )

    # Step A: Layer 3 DB payload merge (when failed import, pause native-sync-added models)
    db_layer = (
        normalized_payload if oauth_on else _filter_payload_for_oauth_disabled(normalized_payload)
    )
    if legacy_sync_import_status == "failed":
        # §2.6: while legacy retired list cannot be confirmed, pause native-sync added models
        # and only allow factory-available (builtin + native_overlay non-factory-retired) models
        # not in any known retired set.
        catalog_variants: dict[str, dict[str, Any]] = {}
    else:
        catalog_variants = _merge_db_payload_layer(db_layer, llm_config, manifest, dalton_meta)

    # Step B: Narrow-field lock on PINNED_OVERLAY_MODEL_KEYS (when OAuth enabled)
    if oauth_on:
        _enforce_narrow_pinned_routes(llm_config, native_overlay)

    # Step C: Three-tier retirement union (factory hard + readonly legacy + team DB)
    readonly_legacy_retired: set[str] = set()
    if legacy_sync_import_status == "failed":
        readonly_legacy_retired.update(
            str(k).strip() for k in (last_known_legacy_retired or []) if str(k).strip()
        )
        readonly_legacy_retired.update(
            str(k).strip() for k in (extra_readonly_legacy_retired or ()) if str(k).strip()
        )
    team_retired_keys: set[str] = set()
    for rec in normalized_payload.get("retired") or []:
        if isinstance(rec, Mapping) and rec.get("key"):
            team_retired_keys.add(str(rec["key"]).strip())

    all_retired_keys = factory_retired_keys | readonly_legacy_retired | team_retired_keys
    if legacy_sync_import_status == "failed":
        # Strictly exclude any non-factory model keys while degraded
        non_factory = [k for k in list(llm_config) if k not in factory_available_keys]
        for k in non_factory:
            llm_config.pop(k, None)
            dalton_meta.pop(k, None)

    if all_retired_keys:
        _drop_retired_models(
            [{"key": k} for k in sorted(all_retired_keys)],
            llm_config,
            manifest,
        )
        for k in all_retired_keys:
            dalton_meta.pop(k, None)

    # Step D: Final capability gate strip & rating inheritance
    if not oauth_on:
        oauth_keys = [
            key
            for key, entry in list(llm_config.items())
            if _is_oauth_key(key)
            or (isinstance(entry, Mapping) and _is_oauth_key(str(entry.get("provider") or "")))
        ]
        if oauth_keys:
            _drop_retired_models([{"key": k} for k in oauth_keys], llm_config, manifest)
        for k in list(dalton_meta):
            if _is_oauth_key(k):
                dalton_meta.pop(k, None)
    else:
        inherit_antigravity_gemini_ratings(llm_config)

    # Build ModelConfig directly from compiled dicts (bypassing disk overlay re-merge)
    stamp = current_overlay_stamp(force=False) or f"rev:{revision}"
    compiled_mc = ModelConfig.from_compiled(
        llm_config=llm_config,
        manifest=manifest,
        overlay_stamp=f"{stamp}|rev:{revision}",
        catalog_revision=revision,
    )

    # Build canonical bound URL map for all catalog-defined provider variants
    variant_bound_urls: dict[str, str] = {}
    for slug, info in compiled_mc.flat_providers.items():
        if info.get("catalog_defined"):
            ck = canonical_url_key(str(info.get("base_url") or ""))
            if ck:
                variant_bound_urls[slug] = ck
            catalog_variants.setdefault(slug, dict(info))

    # Build task costs, notional index, and Antigravity series catalog
    compiled_task_costs = _merge_task_costs(db_layer)
    compiled_mc.task_costs_override = compiled_task_costs

    notional_index = build_notional_price_index_for_catalog(
        model_config=compiled_mc,
        dalton_meta=dalton_meta,
        oauth_sync_enabled=oauth_on,
    )

    series_overrides = (
        dict(db_layer.get("series_overrides") or {})
        if isinstance(db_layer.get("series_overrides"), Mapping)
        else {}
    )
    series_catalog = (
        build_antigravity_series_catalog(
            compiled_mc,
            series_overrides=series_overrides,
            dalton_meta=dalton_meta,
        )
        if oauth_on
        else []
    )

    raw_curated = db_layer.get("curated_setup")
    curated_setup = (
        [str(k).strip() for k in raw_curated if str(k).strip()]
        if isinstance(raw_curated, list)
        else None
    )

    legacy_fp = _compute_retired_fingerprint(
        legacy_sync_import_status,
        list(last_known_legacy_retired or []),
        extra_readonly_legacy_retired,
    )

    return CompiledCatalog(
        revision=int(revision),
        payload_sha256=sha,
        overlay_stamp=stamp,
        oauth_sync_enabled=oauth_on,
        legacy_sync_import_status=legacy_sync_import_status,
        legacy_retired_fingerprint=legacy_fp,
        model_config=compiled_mc,
        dalton_meta=dalton_meta,
        notional_price_index=notional_index,
        task_costs=compiled_task_costs,
        antigravity_series=series_catalog,
        provider_variants=catalog_variants,
        variant_bound_urls=variant_bound_urls,
        factory_retired_keys=frozenset(factory_retired_keys),
        readonly_legacy_retired_keys=frozenset(readonly_legacy_retired),
        team_retired_keys=frozenset(team_retired_keys),
        curated_setup=curated_setup,
        payload=normalized_payload,
        factory_model_keys=factory_available_keys,
        from_db=from_db,
    )


def cache_compiled_catalog(compiled: CompiledCatalog) -> None:
    """Insert ``compiled`` into the bounded per-worker LRU cache and update prewarm watermark."""
    global _HIGHEST_PREWARMED_REVISION
    cache_key = (
        compiled.revision,
        compiled.overlay_stamp,
        compiled.oauth_sync_enabled,
        compiled.legacy_retired_fingerprint,
        compiled.from_db,
    )
    with _CACHE_LOCK:
        _COMPILED_BY_REVISION[cache_key] = compiled
        _COMPILED_BY_REVISION.move_to_end(cache_key)
        while len(_COMPILED_BY_REVISION) > COMPILED_CATALOG_CACHE_MAXSIZE:
            _COMPILED_BY_REVISION.popitem(last=False)
        if compiled.from_db and compiled.revision > _HIGHEST_PREWARMED_REVISION:
            _HIGHEST_PREWARMED_REVISION = compiled.revision


def invalidate_compiled_catalog_cache() -> None:
    """Clear the process-local compiled catalog cache, dependent caches, and prewarm watermark."""
    global _HIGHEST_PREWARMED_REVISION
    with _CACHE_LOCK:
        _COMPILED_BY_REVISION.clear()
        _HIGHEST_PREWARMED_REVISION = -1
    try:
        from src.llms.llm import LLM

        LLM._model_config = None
    except Exception:
        pass
    try:
        from src.server.services.llm.provider_catalog import key_candidates, provider_catalog

        provider_catalog.cache_clear()
        key_candidates.cache_clear()
    except Exception:
        pass


def reset_catalog_caches_for_tests() -> None:
    """Reset all process-local team catalog, ModelConfig, pricing, and contextvar state for tests."""
    invalidate_compiled_catalog_cache()
    try:
        from src.llms import pricing_utils

        pricing_utils._WARNED_MISSING_PRICING.clear()
    except Exception:
        pass
    try:
        from src.dalton.services.team_model_catalog import snapshot as _snap_mod

        _snap_mod._ACTIVE_CATALOG_SNAPSHOT.set(None)
        _snap_mod._ACTIVE_RESUME_VERIFY_TARGET.set(None)
        _snap_mod._ACTIVE_VERIFIED_RESUME_VARIANT_BINDING.set(None)
    except Exception:
        pass


def _get_cached(
    revision: int,
    overlay_stamp: str,
    oauth_sync_enabled: bool,
    legacy_fp: str,
    *,
    from_db: bool = True,
) -> CompiledCatalog | None:
    cache_key = (revision, overlay_stamp, oauth_sync_enabled, legacy_fp, from_db)
    with _CACHE_LOCK:
        found = _COMPILED_BY_REVISION.get(cache_key)
        if found is not None:
            _COMPILED_BY_REVISION.move_to_end(cache_key)
        return found


def _extra_readonly_retired_for_failed(
    status: str,
    last_known_legacy_retired: list[str],
) -> set[str]:
    if status != "failed":
        return set()
    from src.dalton.services.team_model_catalog.legacy_import import (
        extract_disk_legacy_retired_readonly,
    )

    return set(last_known_legacy_retired) | extract_disk_legacy_retired_readonly()


async def get_compiled_catalog_async(
    *,
    conn: Any = None,
    force_reload: bool = False,
) -> CompiledCatalog:
    """Confirm current revision from PostgreSQL and return its cached/compiled ``CompiledCatalog``."""
    from src.dalton.database.team_model_catalog import (
        fetch_legacy_retired_history,
        get_catalog_head,
        get_catalog_row,
    )
    from src.dalton.llm.overlay import _is_native_oauth_sync_enabled, current_overlay_stamp

    oauth_on = _is_native_oauth_sync_enabled()
    stamp = current_overlay_stamp(force=False) or "-"

    head: dict[str, Any] | None = None
    try:
        head = await get_catalog_head(conn=conn)
    except Exception as exc:
        logger.debug("[team_model_catalog] DB head check unavailable; using rev 0 baseline: %s", exc)

    if head is None:
        legacy_fp = _compute_retired_fingerprint("not_applicable", [])
        if not force_reload:
            cached = _get_cached(0, stamp, oauth_on, legacy_fp, from_db=False)
            if cached is not None:
                return cached
        compiled = compile_catalog_from_payload(
            empty_catalog_payload(),
            revision=0,
            oauth_sync_enabled=oauth_on,
            legacy_sync_import_status="not_applicable",
            from_db=False,
        )
        cache_compiled_catalog(compiled)
        return compiled

    rev = int(head["revision"])
    status = str(head["legacy_sync_import_status"])
    known_retired = list(head.get("last_known_legacy_retired") or [])
    extra_readonly = _extra_readonly_retired_for_failed(status, known_retired)
    if status == "failed":
        try:
            extra_readonly |= await fetch_legacy_retired_history(conn=conn)
        except Exception:
            pass
    legacy_fp = _compute_retired_fingerprint(status, known_retired, extra_readonly)

    if not force_reload:
        cached = _get_cached(rev, stamp, oauth_on, legacy_fp, from_db=True)
        if cached is not None:
            return cached

    row = await get_catalog_row(conn=conn)
    if row is None:
        fallback_fp = _compute_retired_fingerprint("not_applicable", [])
        if not force_reload:
            cached = _get_cached(0, stamp, oauth_on, fallback_fp, from_db=False)
            if cached is not None:
                return cached
        compiled = compile_catalog_from_payload(
            empty_catalog_payload(),
            revision=0,
            oauth_sync_enabled=oauth_on,
            legacy_sync_import_status="not_applicable",
            from_db=False,
        )
        cache_compiled_catalog(compiled)
        return compiled

    row_rev = int(row["revision"]) if "revision" in row else rev
    row_status = str(row.get("legacy_sync_import_status") or status)
    row_known_retired = (
        list(row.get("last_known_legacy_retired") or [])
        if "last_known_legacy_retired" in row
        else known_retired
    )
    row_extra_readonly = _extra_readonly_retired_for_failed(row_status, row_known_retired)
    if row_status == "failed":
        try:
            row_extra_readonly |= await fetch_legacy_retired_history(conn=conn)
        except Exception:
            pass
    row_legacy_fp = _compute_retired_fingerprint(row_status, row_known_retired, row_extra_readonly)
    if not force_reload:
        cached = _get_cached(row_rev, stamp, oauth_on, row_legacy_fp)
        if cached is not None:
            return cached

    payload = row.get("payload") or empty_catalog_payload()
    compiled = compile_catalog_from_payload(
        payload,
        revision=row_rev,
        payload_sha256=row.get("payload_sha256"),
        oauth_sync_enabled=oauth_on,
        legacy_sync_import_status=row_status,
        last_known_legacy_retired=row_known_retired,
        extra_readonly_legacy_retired=row_extra_readonly,
    )
    cache_compiled_catalog(compiled)
    return compiled


def get_cached_compiled_catalog_no_io() -> CompiledCatalog | None:
    """Return the most recently compiled catalog in worker memory with zero DB or network I/O."""
    from src.dalton.llm.overlay import _is_native_oauth_sync_enabled, current_overlay_stamp

    oauth_on = _is_native_oauth_sync_enabled()
    stamp = current_overlay_stamp(force=False) or "-"
    with _CACHE_LOCK:
        for cached in reversed(_COMPILED_BY_REVISION.values()):
            if cached.oauth_sync_enabled == oauth_on and cached.overlay_stamp == stamp:
                return cached
    return None


def get_compiled_catalog_sync(
    *, force_reload: bool = False, allow_db: bool = True
) -> CompiledCatalog:
    """Synchronous PG-confirmed catalog lookup for sync call sites (e.g. ``LLM.get_model_config``)."""
    from src.dalton.database.team_model_catalog import get_catalog_head_sync, get_catalog_row_sync
    from src.dalton.llm.overlay import _is_native_oauth_sync_enabled, current_overlay_stamp

    oauth_on = _is_native_oauth_sync_enabled()
    stamp = current_overlay_stamp(force=False) or "-"

    head: dict[str, Any] | None = None
    if allow_db:
        try:
            head = get_catalog_head_sync()
        except Exception as exc:
            logger.debug("[team_model_catalog] sync DB head check skipped: %s", exc)

    if head is None:
        if not allow_db and not force_reload:
            in_mem = get_cached_compiled_catalog_no_io()
            if in_mem is not None:
                return in_mem
        legacy_fp = _compute_retired_fingerprint("not_applicable", [])
        if not force_reload:
            cached = _get_cached(0, stamp, oauth_on, legacy_fp, from_db=False)
            if cached is not None:
                return cached
        compiled = compile_catalog_from_payload(
            empty_catalog_payload(),
            revision=0,
            oauth_sync_enabled=oauth_on,
            legacy_sync_import_status="not_applicable",
            from_db=False,
        )
        cache_compiled_catalog(compiled)
        return compiled

    rev = int(head["revision"])
    status = str(head["legacy_sync_import_status"])
    known_retired = list(head.get("last_known_legacy_retired") or [])
    extra_readonly = _extra_readonly_retired_for_failed(status, known_retired)
    legacy_fp = _compute_retired_fingerprint(status, known_retired, extra_readonly)

    if not force_reload:
        cached = _get_cached(rev, stamp, oauth_on, legacy_fp, from_db=True)
        if cached is not None:
            return cached

    row = get_catalog_row_sync()
    if row is None:
        fallback_fp = _compute_retired_fingerprint("not_applicable", [])
        if not force_reload:
            cached = _get_cached(0, stamp, oauth_on, fallback_fp, from_db=False)
            if cached is not None:
                return cached
        compiled = compile_catalog_from_payload(
            empty_catalog_payload(),
            revision=0,
            oauth_sync_enabled=oauth_on,
            legacy_sync_import_status="not_applicable",
            from_db=False,
        )
        cache_compiled_catalog(compiled)
        return compiled

    row_rev = int(row["revision"]) if "revision" in row else rev
    row_status = str(row.get("legacy_sync_import_status") or status)
    row_known_retired = (
        list(row.get("last_known_legacy_retired") or [])
        if "last_known_legacy_retired" in row
        else known_retired
    )
    row_extra_readonly = _extra_readonly_retired_for_failed(row_status, row_known_retired)
    row_legacy_fp = _compute_retired_fingerprint(row_status, row_known_retired, row_extra_readonly)
    if not force_reload:
        cached = _get_cached(row_rev, stamp, oauth_on, row_legacy_fp)
        if cached is not None:
            return cached

    payload = row.get("payload") or empty_catalog_payload()
    compiled = compile_catalog_from_payload(
        payload,
        revision=row_rev,
        payload_sha256=row.get("payload_sha256"),
        oauth_sync_enabled=oauth_on,
        legacy_sync_import_status=row_status,
        last_known_legacy_retired=row_known_retired,
        extra_readonly_legacy_retired=row_extra_readonly,
    )
    cache_compiled_catalog(compiled)
    return compiled


async def prewarm_compiled_catalog(expected_min_revision: int | None = None) -> CompiledCatalog | None:
    """Monotonically prewarm the local worker cache from PG (never moves watermark backward)."""
    from src.dalton.database.team_model_catalog import get_catalog_head

    try:
        head = await get_catalog_head()
    except Exception as exc:
        logger.debug("[team_model_catalog] prewarm head read failed: %s", exc)
        return None
    if head is None:
        return None
    db_rev = int(head["revision"])
    with _CACHE_LOCK:
        if db_rev <= _HIGHEST_PREWARMED_REVISION:
            if expected_min_revision is None or _HIGHEST_PREWARMED_REVISION >= expected_min_revision:
                return None
    return await get_compiled_catalog_async()
