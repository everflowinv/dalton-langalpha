"""Nominal (reference) token pricing for Dalton subscription and OAuth models.

Subscription and OAuth calls are billed at $0.00 cash (`cost = 0.0`,
`platform_cost = 0.0`), while their reference token value is metered per call as
`notional_cost` so tiered schedules (such as Meta Muse Ember 1.0's 200K input
threshold) evaluate against each individual request rather than aggregated turn
totals.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable

import src.dalton.llm.overlay as _overlay_mod

logger = logging.getLogger(__name__)

_FALLBACK_OAUTH_CARDS: dict[str, dict[str, float]] = {
    "claude-opus-5-5-oauth": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    "claude-opus-5-5-high-oauth": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    "gpt-6-astra-oauth": {"input": 10.0, "output": 50.0, "cached_input": 1.0},
    "gpt-6.1-sol-oauth": {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    "gemini-4-argon-high-oauth": {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    "gemini-3.8-flash-medium-oauth": {"input": 0.75, "output": 3.75, "cached_input": 0.075},
    "gemini-3.8-flash-low-oauth": {"input": 0.75, "output": 3.75, "cached_input": 0.075},
}


def extract_flat_rates_from_card(np: dict[str, Any]) -> dict[str, float]:
    """Extract flat or tier-0 fallback rates `{input, output, cached_input}` from a card."""
    in_rate = float(np.get("input", 0.0) or 0.0)
    out_rate = float(np.get("output", 0.0) or 0.0)
    cache_val = (
        np.get("cached_input")
        if np.get("cached_input") is not None
        else np.get("cache_read", 0.0)
    )
    cache_rate = float(cache_val or 0.0)
    if not in_rate and isinstance(np.get("input_tiers"), list) and np["input_tiers"]:
        tier0 = np["input_tiers"][0]
        if isinstance(tier0, dict):
            in_rate = float(tier0.get("rate", 0.0) or 0.0)
            tier0_cache = (
                tier0.get("cached_input")
                if tier0.get("cached_input") is not None
                else tier0.get("cache_read", cache_rate)
            )
            cache_rate = float(tier0_cache or 0.0)
    if not out_rate and isinstance(np.get("output_tiers"), list) and np["output_tiers"]:
        tier0_out = np["output_tiers"][0]
        if isinstance(tier0_out, dict):
            out_rate = float(tier0_out.get("rate", 0.0) or 0.0)
    return {"input": in_rate, "output": out_rate, "cached_input": cache_rate}


def normalize_notional_card(card: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a nominal pricing card while preserving tiered/matrix structures.

    Flat cards normalize to `{"input": float, "output": float, "cached_input": float}`
    (converting `cache_read` to `cached_input`). Tiered or matrix cards retain their
    full structure (`input_tiers`, `output_tiers`, `tier_matrix`, `input_pricing_mode`,
    `output_pricing_mode`, `unit`, `source`, `verified_at`) so `calculate_total_cost`
    evaluates exact per-call thresholds.
    """
    if not isinstance(card, dict) or not card:
        return None

    has_input_tiers = isinstance(card.get("input_tiers"), list) and bool(card["input_tiers"])
    has_output_tiers = isinstance(card.get("output_tiers"), list) and bool(card["output_tiers"])
    has_tier_matrix = isinstance(card.get("tier_matrix"), list) and bool(card["tier_matrix"])

    if has_input_tiers or has_output_tiers or has_tier_matrix:
        normalized = copy.deepcopy(card)
        tier_has_cached_input = False
        if has_input_tiers:
            for tier in normalized["input_tiers"]:
                if not isinstance(tier, dict):
                    continue
                if tier.get("rate") is not None:
                    tier["rate"] = float(tier["rate"])
                if tier.get("cached_input") is None and tier.get("cache_read") is not None:
                    tier["cached_input"] = float(tier["cache_read"])
                elif tier.get("cached_input") is not None:
                    tier["cached_input"] = float(tier["cached_input"])
                if tier.get("cached_input") is not None:
                    tier_has_cached_input = True
        if has_output_tiers:
            for tier in normalized["output_tiers"]:
                if isinstance(tier, dict) and tier.get("rate") is not None:
                    tier["rate"] = float(tier["rate"])
        if tier_has_cached_input:
            # Ensure top-level cached_input does not shadow per-tier cached_input in get_input_cost()
            normalized.pop("cached_input", None)
            normalized.pop("cache_read", None)
        elif normalized.get("cached_input") is None and normalized.get("cache_read") is not None:
            normalized["cached_input"] = float(normalized["cache_read"])
        normalized.setdefault("unit", "per_1m_tokens")
        return normalized

    in_val = card.get("input")
    out_val = card.get("output")
    if in_val is None and out_val is None:
        return None
    in_f = float(in_val or 0.0)
    out_f = float(out_val or 0.0)
    cache_val = (
        card.get("cached_input")
        if card.get("cached_input") is not None
        else card.get("cache_read", 0.0)
    )
    cache_f = float(cache_val or 0.0)
    return {"input": in_f, "output": out_f, "cached_input": cache_f}


def build_notional_price_index(
    overlay_loader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build `{model_key_or_ref: normalized_notional_card}` for subscription/OAuth models."""
    loader = overlay_loader or _overlay_mod.load_overlay_data
    out: dict[str, dict[str, Any]] = {}

    overlay = loader() or {}
    models_map = overlay.get("models") or {}
    prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
        "openclaw-broker", []
    )
    by_ref: dict[str, dict[str, Any]] = {}
    for pm in prov_models:
        if isinstance(pm, dict) and pm.get("id"):
            by_ref[str(pm["id"])] = pm

    for key, mcfg in models_map.items():
        if not isinstance(mcfg, dict):
            continue
        ref = str(mcfg.get("model_id") or "")
        pm = by_ref.get(ref) or {}
        np = pm.get("notional_pricing")
        if isinstance(np, dict):
            card = normalize_notional_card(np)
            if card is not None:
                out[str(key)] = card
                if ref:
                    out[ref] = card

    try:
        from src.llms.pricing_utils import find_model_pricing, resolve_pricing_identity

        dalton_meta = _overlay_mod.load_dalton_meta() or {}
        native_overlay = loader(_overlay_mod.NATIVE_OVERLAY_PATH) or {}
        native_models = native_overlay.get("models") or {}

        for key, meta_entry in dalton_meta.items():
            if not isinstance(meta_entry, dict):
                continue
            card: dict[str, Any] | None = None
            np = meta_entry.get("notional_pricing")
            if isinstance(np, dict):
                card = normalize_notional_card(np)
            elif meta_entry.get("notional_ref"):
                ref_id = str(meta_entry["notional_ref"])
                if ref_id in out:
                    card = copy.deepcopy(out[ref_id])
                else:
                    ref_prov, ref_pricing_id = resolve_pricing_identity(ref_id)
                    raw_card = find_model_pricing(ref_pricing_id, provider=ref_prov)
                    if raw_card is None and ref_pricing_id != ref_id:
                        raw_card = find_model_pricing(ref_id)
                    if isinstance(raw_card, dict):
                        card = normalize_notional_card(raw_card)
            if card is not None:
                out[str(key)] = card
                m_entry = native_models.get(key) if isinstance(native_models, dict) else None
                if isinstance(m_entry, dict) and m_entry.get("model_id"):
                    mid = str(m_entry["model_id"])
                    if (dalton_meta.get(mid) or {}).get("billing") != "cash":
                        out.setdefault(mid, card)
    except Exception as exc:
        logger.debug("[DaltonNotional] build_notional_price_index dalton_meta lookup skipped: %s", exc)

    for k, fallback_card in _FALLBACK_OAUTH_CARDS.items():
        out.setdefault(k, dict(fallback_card))
    return out


def is_subscription_model(
    model_name: str,
    billing_type: str = "",
    *,
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
    overlay_loader: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    """Return True when the model or route uses subscription/OAuth billing."""
    if billing_type == "oauth" or model_name.endswith("-oauth"):
        return True
    if pricing_model_id and pricing_model_id.endswith("-oauth"):
        return True
    if pricing_provider and (
        pricing_provider == "z-ai-cn-coding" or pricing_provider.endswith("-oauth")
    ):
        return True

    dalton_meta = _overlay_mod.load_dalton_meta() or {}
    for candidate in (model_name, pricing_model_id):
        if not candidate:
            continue
        meta = dalton_meta.get(candidate)
        if isinstance(meta, dict):
            billing = str(meta.get("billing") or "")
            if billing == "subscription":
                return True
            if billing == "cash":
                return False
        oauth_meta = dalton_meta.get(f"{candidate}-oauth")
        if isinstance(oauth_meta, dict) and str(oauth_meta.get("billing") or "") == "subscription":
            return True

    if model_name.startswith("ocb-"):
        try:
            loader = overlay_loader or _overlay_mod.load_overlay_data
            overlay = loader() or {}
            mcfg = (overlay.get("models") or {}).get(model_name)
            if isinstance(mcfg, dict):
                ref = str(mcfg.get("model_id") or "")
                prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
                    "openclaw-broker", []
                )
                for pm in prov_models:
                    if isinstance(pm, dict) and str(pm.get("id")) == ref:
                        if (pm.get("pricing") or {}).get("pricing_type") == "subscription" or (
                            pm.get("dalton") or {}
                        ).get("billing") == "subscription":
                            return True
        except Exception:
            pass

    return False


def resolve_notional_card(
    model_name: str,
    *,
    pricing_model_id: str | None = None,
    index: dict[str, dict[str, Any]] | None = None,
    overlay_loader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve the normalized nominal pricing card for a model or stamped pricing id."""
    idx = index if index is not None else build_notional_price_index(overlay_loader=overlay_loader)
    for candidate in (
        model_name,
        pricing_model_id,
        f"{model_name}-oauth" if model_name and not model_name.endswith("-oauth") else None,
        (
            f"{pricing_model_id}-oauth"
            if pricing_model_id and not pricing_model_id.endswith("-oauth")
            else None
        ),
    ):
        if candidate and candidate in idx:
            return copy.deepcopy(idx[candidate])
    return None


def calculate_notional_cost(
    card: dict[str, Any] | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cache_5m_tokens: int = 0,
    cache_1h_tokens: int = 0,
    legacy_aggregate_call_count: int = 1,
) -> tuple[float, dict[str, Any]]:
    """Calculate nominal USD cost and breakdown for a single call (or legacy aggregate).

    Uses `calculate_total_cost` from `src.llms.pricing_utils` so tiered and
    `input_dependent` schedules evaluate against the request's total input tokens
    (including cached tokens). When `legacy_aggregate_call_count > 1` on a legacy
    row that lacks `per_call_costs`, falls back to tier-0 flat rates rather than
    feeding multi-call summed tokens into a threshold check.
    """
    if not isinstance(card, dict) or not card:
        return 0.0, {}

    from src.llms.pricing_utils import calculate_total_cost

    effective_card = card
    if legacy_aggregate_call_count > 1 and (
        card.get("input_tiers") or card.get("output_tiers") or card.get("tier_matrix")
    ):
        effective_card = extract_flat_rates_from_card(card)

    res = calculate_total_cost(
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cached_tokens=int(cached_tokens or 0),
        cache_5m_tokens=int(cache_5m_tokens or 0),
        cache_1h_tokens=int(cache_1h_tokens or 0),
        pricing=effective_card,
    )
    return float(res.get("total_cost") or 0.0), dict(res.get("breakdown") or {})
