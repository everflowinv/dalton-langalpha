"""Strict Pydantic white-list schemas and deterministic pre-save validation (§3.1, §3.2)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.credentials import (
    find_disallowed_routing_header_keys,
    find_disallowed_routing_param_keys,
)
from src.dalton.llm.native_catalog import assert_no_credentials
from src.dalton.llm.overlay import _is_oauth_model_key
from src.dalton.services.team_model_catalog.constants import (
    CatalogConflictError,
    CatalogValidationError,
    norm_id,
)
from src.dalton.services.team_model_catalog.transport import validate_https_variant_base_url
from src.llms.llm import CUSTOM_MODEL_NAME_RE
from src.llms.model_spec import ModelSpec, wire_api
from src.llms.reasoning import REASONING_LEVELS, ReasoningSurfaceError, validate_surface

_MODEL_KEY_RE = re.compile(CUSTOM_MODEL_NAME_RE)
_PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VALID_MODALITIES = frozenset({"text", "image", "pdf", "audio", "video"})
_MIN_CONTEXT = 1_000
_MAX_CONTEXT = 100_000_000
_MAX_RATE = 10_000.0
def _discover_supported_variant_sdks() -> frozenset[str]:
    """Derive non-CLI variant SDK names directly from ``LLM._get_*_llm`` builders."""
    from src.llms.llm import LLM

    excluded = frozenset({"codex"})
    prefix, suffix = "_get_", "_llm"
    discovered = {
        name[len(prefix) : -len(suffix)]
        for name in dir(LLM)
        if name.startswith(prefix)
        and name.endswith(suffix)
        and len(name) > len(prefix) + len(suffix)
    }
    return frozenset(discovered - excluded)


_SUPPORTED_VARIANT_SDKS = _discover_supported_variant_sdks()


class PricingMetaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["official", "estimated", "third_party"] = "official"
    estimated: bool | None = None
    source: str | None = Field(default=None, max_length=280)
    source_url: str | None = Field(default=None, max_length=512)
    verified_at: str | None = None
    note: str | None = Field(default=None, max_length=280)

    @field_validator("verified_at")
    @classmethod
    def _validate_date(cls, v: str | None) -> str | None:
        if v is not None and not _DATE_RE.match(v):
            raise ValueError("verified_at must be YYYY-MM-DD")
        return v


class PricingTierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = None
    rate: float
    cached_input: float | None = None
    cache_read: float | None = None


class PricingCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float | None = None
    output: float | None = None
    cached_input: float | None = None
    cache_read: float | None = None
    cache_5m: float | None = None
    cache_1h: float | None = None
    unit: str = "per_1m_tokens"
    input_tiers: list[PricingTierInput] | None = None
    output_tiers: list[PricingTierInput] | None = None
    input_pricing_mode: str | None = None
    output_pricing_mode: str | None = None
    source_kind: Literal["official", "estimated", "third_party"] | None = None
    estimated: bool | None = None
    source: str | None = Field(default=None, max_length=280)
    source_url: str | None = Field(default=None, max_length=512)
    verified_at: str | None = None
    note: str | None = Field(default=None, max_length=280)


class ProviderVariantUpsertInput(BaseModel):
    """Explicit whitelist for catalog-defined provider variants (§3.1 #2)."""

    model_config = ConfigDict(extra="forbid")

    provider_slug: str = Field(min_length=2, max_length=63)
    display_name: str = Field(min_length=1, max_length=120)
    sdk: str = Field(min_length=1, max_length=32)
    parent_provider: str = Field(min_length=1, max_length=63)
    access_type: Literal["api_key", "coding_plan"] = "api_key"
    billing: Literal["cash", "subscription"] = "cash"
    base_url: str = Field(min_length=8, max_length=512)
    # Write-only credential input (stored exclusively in dalton_catalog_provider_credentials)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class DaltonMetaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str | None = Field(default=None, max_length=64)
    billing: Literal["cash", "subscription"] | None = None
    tool_calling: bool | None = None
    forbidden_for_roles: list[str] | None = None
    notional_pricing: PricingCardInput | None = None
    notional_ref: str | None = None
    series: str | None = Field(default=None, max_length=120)
    awaiting_release: bool | None = None


class CatalogModelUpsertInput(BaseModel):
    """Explicit whitelist for creating/updating a team catalog model (§3.1 #2)."""

    model_config = ConfigDict(extra="forbid")

    model_key: str | None = None
    model_id: str | None = None
    provider: str | None = None
    display_name: str | None = Field(default=None, max_length=160)
    visible: bool | None = None
    prompt_guidance: str | None = None
    intelligence: int | None = None
    speed: int | None = None
    plain_language: int | None = None
    context: int | None = None
    compaction_profile: str | None = None
    input_modalities: list[str] | None = None
    parameters: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    reasoning: dict[str, Any] | None = None
    pricing: PricingCardInput | None = None
    pricing_meta: PricingMetaInput | None = None
    alias: list[str] | None = None
    dalton_meta: DaltonMetaInput | None = None
    task_costs_bound: dict[str, Any] | None = None
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class ModelVisibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class ModelRetireInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    successor_model: str | None = None
    migrate_user_defaults: bool = False
    clear_subagent_model: bool = False
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reject_conflicting_subagent_options(self) -> "ModelRetireInput":
        if self.clear_subagent_model and self.successor_model and self.successor_model.strip():
            raise CatalogValidationError(
                "conflicting_retire_options",
                "clear_subagent_model=true cannot be combined with successor_model.",
            )
        return self


class ModelRestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class CuratedSetupUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    curated_setup: list[str] | None
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class RollbackVariantCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=8, max_length=512)
    api_key: str = Field(min_length=1, max_length=4096)


class CatalogRollbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_revision: int = Field(ge=0)
    expected_revision: int | None = None
    variant_credentials: dict[str, RollbackVariantCredentialInput] = Field(default_factory=dict)
    successor_for_dropped: dict[str, str] = Field(default_factory=dict)
    comment: str | None = Field(default=None, max_length=500)


class LegacyImportResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["retry_import", "abandon_legacy_file"]
    persist_known_retired: bool = True
    expected_revision: int | None = None
    comment: str | None = Field(default=None, max_length=500)


class CatalogTestCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_key: str | None = None
    provider: str = Field(min_length=1, max_length=63)
    model_id: str = Field(min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    sdk: str | None = None
    parameters: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None


def _check_rate_number(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogValidationError("invalid_pricing", f"{label} must be a finite number")
    val = float(value)
    if not math.isfinite(val) or val < 0.0 or val > _MAX_RATE:
        raise CatalogValidationError(
            "invalid_pricing",
            f"{label} must be a finite number in [0, {_MAX_RATE}] (got {value!r})",
        )


def validate_pricing_card_numbers(card: Mapping[str, Any], label: str) -> None:
    """Validate finite rate bounds ``[0, 10000]`` and strictly increasing tier ``max_tokens``."""
    for field_name in ("input", "output", "cached_input", "cache_read", "cache_5m", "cache_1h"):
        _check_rate_number(card.get(field_name), f"{label}.{field_name}")

    for tiers_key in ("input_tiers", "output_tiers"):
        tiers = card.get(tiers_key)
        if tiers is None:
            continue
        if not isinstance(tiers, list) or not tiers:
            raise CatalogValidationError(
                "invalid_pricing", f"{label}.{tiers_key} must be a non-empty list"
            )
        prev_max = 0
        for idx, tier in enumerate(tiers):
            if not isinstance(tier, Mapping):
                raise CatalogValidationError(
                    "invalid_pricing", f"{label}.{tiers_key}[{idx}] must be an object"
                )
            _check_rate_number(tier.get("rate"), f"{label}.{tiers_key}[{idx}].rate")
            _check_rate_number(
                tier.get("cached_input"), f"{label}.{tiers_key}[{idx}].cached_input"
            )
            _check_rate_number(tier.get("cache_read"), f"{label}.{tiers_key}[{idx}].cache_read")
            max_tok = tier.get("max_tokens")
            is_last = idx == len(tiers) - 1
            if is_last:
                if max_tok is not None:
                    raise CatalogValidationError(
                        "invalid_pricing",
                        f"{label}.{tiers_key} final tier must have max_tokens=null",
                    )
            else:
                if isinstance(max_tok, bool) or not isinstance(max_tok, int) or max_tok <= prev_max:
                    raise CatalogValidationError(
                        "invalid_pricing",
                        f"{label}.{tiers_key}[{idx}].max_tokens must be strictly increasing integers",
                    )
                prev_max = max_tok


def validate_no_forbidden_params_or_credentials(obj: Any, context: str) -> None:
    """Reject endpoint/connection/vertex params, routing headers, and credential keys."""
    try:
        assert_no_credentials(obj, path=context)
    except ValueError as exc:
        raise CatalogValidationError("forbidden_credential_field", str(exc)) from exc

    if isinstance(obj, Mapping):
        params = obj.get("parameters")
        if isinstance(params, Mapping):
            bad_params = find_disallowed_routing_param_keys(params)
            bad_headers = find_disallowed_routing_header_keys(params)
            if bad_params or bad_headers:
                bad = sorted(set(bad_params) | set(bad_headers))
                raise CatalogValidationError(
                    "forbidden_routing_parameters",
                    f"{context} cannot set endpoint, connection, Vertex, or routing header parameters: {', '.join(bad)}",
                    extra={"fields": bad},
                )


def validate_provider_variant_input(
    data: ProviderVariantUpsertInput,
    *,
    compiled: Any,
    is_update: bool = False,
) -> tuple[str, str]:
    """Validate a provider variant upsert against cross-group uniqueness and HTTPS rules."""
    slug = data.provider_slug.strip()
    if not _PROVIDER_SLUG_RE.match(slug):
        raise CatalogValidationError(
            "invalid_provider_slug",
            f"provider_slug {slug!r} must match [a-z0-9][a-z0-9_-]{{1,62}}",
        )
    oauth_on = has_capability(CapabilityId.NATIVE_OAUTH_SYNC)
    if not oauth_on and (
        _is_oauth_model_key(slug.lower()) or _is_oauth_model_key(data.parent_provider.lower())
    ):
        raise CatalogValidationError(
            "oauth_capability_disabled",
            "OAuth provider variants cannot be configured when native_oauth_sync is disabled.",
        )
    if data.sdk not in _SUPPORTED_VARIANT_SDKS:
        raise CatalogValidationError(
            "unsupported_sdk",
            f"Unsupported catalog variant SDK {data.sdk!r}; allowed: {sorted(_SUPPORTED_VARIANT_SDKS)}",
        )
    try:
        cleaned_url, canonical_bound = validate_https_variant_base_url(data.base_url)
    except ValueError as exc:
        raise CatalogValidationError("invalid_base_url", str(exc)) from exc

    mc = compiled.model_config
    prov_cfg = (mc.manifest or {}).get("provider_config") or {}
    if data.parent_provider not in prov_cfg:
        raise CatalogValidationError(
            "unknown_parent_provider",
            f"Unknown parent_provider group {data.parent_provider!r}",
        )

    # Dimension ②: cross-group uniqueness across all top-level groups & flat variants
    existing_flat_lower = {k.lower(): k for k in mc.flat_providers}
    existing_groups_lower = {k.lower(): k for k in prov_cfg}
    if slug.lower() in existing_groups_lower:
        raise CatalogConflictError(
            "provider_slug_conflict",
            f"Provider slug {slug!r} conflicts with top-level provider group {existing_groups_lower[slug.lower()]!r}",
        )
    existing_key = existing_flat_lower.get(slug.lower())
    if existing_key is not None:
        existing_info = mc.get_provider_info(existing_key)
        if not existing_info.get("catalog_defined") or not is_update:
            raise CatalogConflictError(
                "provider_slug_conflict",
                f"Provider slug {slug!r} already exists in the catalog.",
            )

    if not is_update and not (data.api_key and data.api_key.strip()):
        raise CatalogValidationError(
            "variant_api_key_required",
            f"Creating catalog provider variant {slug!r} requires a dedicated api_key.",
        )
    return cleaned_url, canonical_bound


def _pricing_card_has_nonzero_rate(card: Mapping[str, Any]) -> bool:
    for field_name in ("input", "output", "cached_input", "cache_read", "cache_5m", "cache_1h"):
        val = card.get(field_name)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) > 0.0:
            return True
    for tiers_key in ("input_tiers", "output_tiers"):
        tiers = card.get(tiers_key)
        if not isinstance(tiers, list):
            continue
        for tier in tiers:
            if not isinstance(tier, Mapping):
                continue
            for tf in ("rate", "cached_input", "cache_read"):
                val = tier.get(tf)
                if isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) > 0.0:
                    return True
    return False


def validate_model_fields(
    *,
    model_key: str,
    model_entry: Mapping[str, Any],
    pricing_card: Mapping[str, Any] | None,
    dalton_meta_entry: Mapping[str, Any] | None,
    provider_info: Mapping[str, Any],
    pricing_explicit: bool = False,
) -> None:
    """Validate naming, numeric bounds, ratings, modalities, and reasoning surface for one model."""
    if not _MODEL_KEY_RE.match(model_key):
        raise CatalogValidationError(
            "invalid_model_key",
            f"model_key {model_key!r} is invalid (must match {CUSTOM_MODEL_NAME_RE})",
        )
    oauth_on = has_capability(CapabilityId.NATIVE_OAUTH_SYNC)
    provider = str(model_entry.get("provider") or "").strip()
    if not oauth_on and (
        _is_oauth_model_key(model_key.lower()) or _is_oauth_model_key(provider.lower())
    ):
        raise CatalogValidationError(
            "oauth_capability_disabled",
            "OAuth models cannot be created or edited when native_oauth_sync is disabled.",
        )

    validate_no_forbidden_params_or_credentials(model_entry, f"models.{model_key}")
    if dalton_meta_entry:
        validate_no_forbidden_params_or_credentials(dalton_meta_entry, f"dalton_meta.{model_key}")

    # Context & max_tokens bounds
    context = model_entry.get("context")
    if context is not None:
        if isinstance(context, bool) or not isinstance(context, int) or not (
            _MIN_CONTEXT <= context <= _MAX_CONTEXT
        ):
            raise CatalogValidationError(
                "invalid_context",
                f"context must be an integer in [{_MIN_CONTEXT}, {_MAX_CONTEXT}]",
            )
    params = model_entry.get("parameters") if isinstance(model_entry.get("parameters"), Mapping) else {}
    max_tok = params.get("max_tokens") or params.get("max_output_tokens")
    if max_tok is not None:
        if isinstance(max_tok, bool) or not isinstance(max_tok, int) or max_tok <= 0:
            raise CatalogValidationError("invalid_max_tokens", "max_tokens must be a positive integer")
        if context is not None and max_tok > context:
            raise CatalogValidationError(
                "invalid_max_tokens",
                f"max_tokens ({max_tok}) cannot exceed context ({context})",
            )

    # Ratings 1..5 or None
    for rating_field in ("intelligence", "speed", "plain_language"):
        rv = model_entry.get(rating_field)
        if rv is not None and (isinstance(rv, bool) or not isinstance(rv, int) or not (1 <= rv <= 5)):
            raise CatalogValidationError(
                "invalid_rating",
                f"{rating_field} must be an integer in 1..5 or null",
            )

    # Modalities
    mods = model_entry.get("input_modalities")
    if mods is not None:
        if not isinstance(mods, list) or not mods or any(m not in _VALID_MODALITIES for m in mods):
            raise CatalogValidationError(
                "invalid_modalities",
                f"input_modalities must be a non-empty subset of {sorted(_VALID_MODALITIES)}",
            )

    # Reasoning surface + wire_api validation
    reasoning = model_entry.get("reasoning")
    if isinstance(reasoning, Mapping) and reasoning:
        efforts = reasoning.get("efforts")
        if efforts is not None:
            if not isinstance(efforts, list) or any(e not in REASONING_LEVELS for e in efforts):
                raise CatalogValidationError(
                    "invalid_reasoning",
                    f"reasoning.efforts must be drawn from {list(REASONING_LEVELS)}",
                )
        try:
            target_wire = wire_api(dict(provider_info), model_entry.get("use_response_api"))
            validate_surface(f"models.{model_key}", dict(reasoning), wire=target_wire)
        except ReasoningSurfaceError as exc:
            raise CatalogValidationError("invalid_reasoning_surface", str(exc)) from exc

    # Pricing numbers & billing consistency
    prov_billing = str(provider_info.get("billing") or "").strip().lower()
    meta_billing = str((dalton_meta_entry or {}).get("billing") or "").strip().lower()
    if prov_billing == "subscription" and meta_billing == "cash":
        raise CatalogValidationError(
            "invalid_billing",
            f"Model {model_key!r} on subscription provider {provider!r} cannot override billing to 'cash'.",
        )
    is_subscription_route = (
        prov_billing == "subscription"
        or meta_billing == "subscription"
        or _is_oauth_model_key(provider.lower())
        or provider == "z-ai-cn-coding"
    )
    billing = "subscription" if is_subscription_route else "cash"
    if pricing_card is not None:
        validate_pricing_card_numbers(pricing_card, f"pricing.{model_key}")
        if is_subscription_route and (
            pricing_explicit or _pricing_card_has_nonzero_rate(pricing_card)
        ):
            raise CatalogValidationError(
                "subscription_cash_pricing_forbidden",
                f"Subscription-billed model {model_key!r} cannot configure cash pricing rates; "
                "use dalton_meta.notional_pricing instead.",
            )
    elif billing == "cash":
        raise CatalogValidationError(
            "missing_cash_pricing",
            f"Cash-billed model {model_key!r} requires a valid cash pricing card.",
        )

    if isinstance(dalton_meta_entry, Mapping):
        np = dalton_meta_entry.get("notional_pricing")
        if isinstance(np, Mapping):
            validate_pricing_card_numbers(np, f"notional_pricing.{model_key}")


def trial_compile_and_validate_catalog(
    candidate_payload: Mapping[str, Any],
    *,
    revision: int,
    oauth_sync_enabled: bool | None = None,
    legacy_sync_import_status: str = "not_applicable",
    last_known_legacy_retired: list[str] | None = None,
) -> Any:
    """Trial-compile ``candidate_payload`` in memory and verify all 4-dimension & notional invariants."""
    from src.dalton.llm.notional_pricing import check_notional_pricing_ambiguity
    from src.dalton.services.team_model_catalog.compiled import compile_catalog_from_payload

    validate_no_forbidden_params_or_credentials(candidate_payload, "catalog.payload")

    compiled = compile_catalog_from_payload(
        candidate_payload,
        revision=revision,
        oauth_sync_enabled=oauth_sync_enabled,
        legacy_sync_import_status=legacy_sync_import_status,
        last_known_legacy_retired=last_known_legacy_retired,
    )
    mc = compiled.model_config

    # Dimension ①: case-insensitive model key uniqueness
    seen_keys_lower: dict[str, str] = {}
    for key in mc.llm_config:
        low = str(key).lower()
        if low in seen_keys_lower:
            raise CatalogConflictError(
                "duplicate_model_key",
                f"Duplicate model key (case-insensitive): {seen_keys_lower[low]!r} vs {key!r}",
            )
        seen_keys_lower[low] = str(key)

    # Dimension ③: per-provider pricing card norm_id(id) & norm_id(alias) disjointness
    manifest_models = (mc.manifest or {}).get("models") or {}
    for prov, cards in manifest_models.items():
        if not isinstance(cards, list):
            continue
        seen_ids: dict[str, str] = {}
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            cid = str(card.get("id") or "").strip()
            if not cid:
                continue
            card_tokens: list[str] = [cid]
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                card_tokens.extend(str(a).strip() for a in aliases if str(a).strip())
            card_norms: set[str] = set()
            for tok in card_tokens:
                nt = norm_id(tok)
                if nt in card_norms:
                    continue
                if nt in seen_ids:
                    raise CatalogConflictError(
                        "pricing_card_id_conflict",
                        f"Provider {prov!r} pricing card identifier {tok!r} (normalized {nt!r}) "
                        f"collides with card {seen_ids[nt]!r}.",
                    )
                seen_ids[nt] = cid
                card_norms.add(nt)

    # Trial-build ModelSpec for every DB-touched model key
    touched_keys = set((candidate_payload.get("models") or {}).keys()) | set(
        (candidate_payload.get("model_overrides") or {}).keys()
    )
    for key in touched_keys:
        if key in mc.llm_config:
            try:
                ModelSpec.from_manifest(mc, key)
            except Exception as exc:
                raise CatalogValidationError(
                    "trial_compile_failed",
                    f"Model {key!r} failed ModelSpec trial compilation: {exc}",
                ) from exc

    # Notional pricing (provider, norm_id(model_id)) ambiguity check (§2.2 #1)
    conflicts = check_notional_pricing_ambiguity(
        model_config=mc,
        dalton_meta=compiled.dalton_meta,
    )
    if conflicts:
        raise CatalogValidationError(
            "ambiguous_notional_pricing",
            "Multiple active models under the same provider share a normalized model_id with "
            "conflicting notional pricing cards.",
            extra={"conflicts": conflicts},
        )

    return compiled
