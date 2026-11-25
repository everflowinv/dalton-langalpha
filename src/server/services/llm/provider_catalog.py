"""What the setup wizard and the key picker know about providers, published
by ``GET /api/v1/models``. Built once per process: the manifest is static."""

from __future__ import annotations

from typing import Any  # DALTON-HOOK(team-model-catalog)


def _base_url(info: dict) -> str | None:
    from src.config.env import HOST_IP

    return (info.get("base_url") or "").replace("{HOST_IP}", HOST_IP) or None


def build_provider_catalog(config) -> list[dict]:
    """DALTON-HOOK(team-model-catalog): build wizard-visible provider catalog from a pinned ModelConfig."""
    from src.config.settings import HOST_MODE

    flat = config.flat_providers

    # Another API of a connection, saved under by a model served on it.
    routes_by_parent: dict[str, list[str]] = {}
    for key, info in flat.items():
        if not info.get("platform") and config.connection_of(key) != key:
            routes_by_parent.setdefault(config.connection_of(key), []).append(key)

    region_variants_by_parent: dict[str, list[dict]] = {}
    for key, info in flat.items():
        if info.get("platform"):
            continue
        parent_key = info.get("parent_provider")
        if not parent_key:
            continue
        parent_info = flat.get(parent_key, {})
        if (
            info.get("access_type", "api_key") == parent_info.get("access_type", "api_key")
            and info.get("region") != parent_info.get("region")
        ):
            region_variants_by_parent.setdefault(parent_key, []).append({
                "provider": key,
                "display_name": config.get_display_name(key),
                "region": info.get("region", "intl"),
                "sdk": info.get("sdk"),
                "base_url": _base_url(info),
                "use_response_api": info.get("use_response_api", False),
                "models_from": config.models_from(key),
            })

    def _entry(k: str, inf: dict, brand: str) -> dict:
        group = inf.get("group", k)
        entry = {
            "provider": k,
            "display_name": config.get_display_name(k),
            "access_type": inf.get("access_type", "api_key"),
            "brand_key": brand,
            # Eligible unless a provider says otherwise, the same default
            # ``get_byok_eligible_providers`` reads it with. Defaulting to False
            # here reported ten real providers as ineligible, the local three
            # among them, while the resolver was happily accepting keys for them.
            "byok_eligible": inf.get("byok_eligible", True),
            "region": inf.get("region", "intl"),
            "sdk": inf.get("sdk"),
            "base_url": _base_url(inf),
            "use_response_api": inf.get("use_response_api", False),
            "dynamic_models": inf.get("dynamic_models", False),
            # Whose manifest models a key saved here offers (``models_from``):
            # the provider's own, else a plan's brand, or a plan region's plan.
            "models_from": config.models_from(k),
            "group": group,
            "group_display_name": config.get_display_name(group),
            # What kind of provider the group is (closed_source, open_source,
            # inference), which the key picker sorts providers by.
            "category": inf.get("category"),
        }
        rv = region_variants_by_parent.get(k)
        if rv:
            entry["region_variants"] = rv
        if routes_by_parent.get(k):
            entry["routes"] = routes_by_parent[k]
        return entry

    catalog = []
    for key, info in flat.items():
        if info.get("platform"):
            continue
        # Local providers (Ollama, LM Studio, vLLM) are OSS-only
        if info.get("access_type") == "local" and HOST_MODE == "platform":
            continue
        if not config.provider_listed(info):
            continue
        parent_key = info.get("parent_provider")
        if not parent_key or info.get("access_type", "api_key") != flat.get(parent_key, {}).get("access_type", "api_key"):
            catalog.append(_entry(key, info, config.brand_of(key)))
    return catalog


# DALTON-HOOK(team-model-catalog): key provider_catalog and key_candidates caches to the live
# ModelConfig instance/revision so team catalog updates invalidate immediately within the worker
# while preserving `.cache_clear()` compatibility for callers and tests.
_PROVIDER_CATALOG_CACHE: tuple[Any, list[dict]] | None = None
_KEY_CANDIDATES_CACHE: tuple[Any, dict[str, dict]] | None = None


def _clear_provider_catalog_cache() -> None:
    global _PROVIDER_CATALOG_CACHE
    _PROVIDER_CATALOG_CACHE = None


def _clear_key_candidates_cache() -> None:
    global _KEY_CANDIDATES_CACHE
    _KEY_CANDIDATES_CACHE = None


def provider_catalog() -> list[dict]:
    """Wizard-visible providers.

    A provider is wizard-visible if it has no parent_provider (a root brand),
    or its access_type differs from its parent's. Region variants (same
    access_type, different region) are attached as ``region_variants`` on the
    parent's entry so the wizard can show an inline region toggle.
    """
    global _PROVIDER_CATALOG_CACHE
    from src.llms.llm import LLM

    mc = LLM.get_model_config()
    cached = _PROVIDER_CATALOG_CACHE
    if cached is not None and cached[0] is mc:
        return cached[1]
    result = build_provider_catalog(mc)
    _PROVIDER_CATALOG_CACHE = (mc, result)
    return result


provider_catalog.cache_clear = _clear_provider_catalog_cache  # type: ignore[attr-defined]


def build_key_candidates(mc) -> dict[str, dict]:
    """DALTON-HOOK(team-model-catalog): build key_candidates map from a pinned ModelConfig."""
    from src.server.services.llm.byok import byok_candidates

    out = {}
    for key, info in mc.flat_providers.items():
        if info.get("platform") or info.get("access_type") == "oauth":
            continue
        slugs = byok_candidates(key, mc)
        out[key] = {"slugs": slugs, "shared": [s for s in slugs if mc.shares_api_key(key, s)]}
    return out


def key_candidates() -> dict[str, dict]:
    """Where a turn looks for each provider's stored key, so the client can
    tell which saved models the user's own keys serve without re-deriving the
    walk: ``slugs`` in ``byok_candidates`` order, ``shared`` the ones whose
    key the vendor issues as one with the provider's. A key under another slug
    serves it only when saved with a base URL; a custom model takes any.
    """
    global _KEY_CANDIDATES_CACHE
    from src.llms.llm import LLM

    mc = LLM.get_model_config()
    cached = _KEY_CANDIDATES_CACHE
    if cached is not None and cached[0] is mc:
        return cached[1]
    result = build_key_candidates(mc)
    _KEY_CANDIDATES_CACHE = (mc, result)
    return result


key_candidates.cache_clear = _clear_key_candidates_cache  # type: ignore[attr-defined]
