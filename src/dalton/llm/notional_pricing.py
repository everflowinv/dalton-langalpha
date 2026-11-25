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

_DEFAULT_LOAD_DALTON_META = _overlay_mod.load_dalton_meta

logger = logging.getLogger(__name__)

_FALLBACK_OAUTH_CARDS: dict[str, dict[str, float]] = {
    "claude-opus-5-5-oauth": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    "claude-opus-5-5-high-oauth": {"input": 5.0, "output": 25.0, "cached_input": 0.5},
    "gpt-6-astra-oauth": {"input": 10.0, "output": 50.0, "cached_input": 1.0},
    "gpt-6.1-sol-oauth": {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    "gpt-6.2-astra-oauth": {"input": 8.0, "output": 40.0, "cached_input": 0.4},
    "gpt-6.2-sol-oauth": {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    "gemini-4-argon-high-oauth": {"input": 2.0, "output": 10.0, "cached_input": 0.1},
    "gemini-4-neon-low-oauth": {"input": 3.0, "output": 15.0, "cached_input": 0.15},
    "gemini-4-neon-medium-oauth": {"input": 3.0, "output": 15.0, "cached_input": 0.15},
    "gemini-4-neon-high-oauth": {"input": 3.0, "output": 15.0, "cached_input": 0.15},
    "gemini-4-neon-xhigh-oauth": {"input": 3.0, "output": 15.0, "cached_input": 0.15},
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


def notional_pair_key(provider: str | None, model_id: str | None) -> str:
    """Canonical ``(pricing_provider.lower(), norm_id(pricing_model_id))`` index key (§2.2 #1)."""
    prov = str(provider or "").strip().lower()
    mid = str(model_id or "").strip().lower().replace(".", "-")
    return f"{prov}\0{mid}"


def _canonical_card_signature(card: dict[str, Any] | None) -> str:
    """Deterministic comparable signature of rate fields on a normalized notional card."""
    import json

    if not isinstance(card, dict):
        return ""
    rate_fields = {
        k: v
        for k, v in card.items()
        if k
        in (
            "input",
            "output",
            "cached_input",
            "cache_5m",
            "cache_1h",
            "input_tiers",
            "output_tiers",
            "tier_matrix",
            "input_pricing_mode",
            "output_pricing_mode",
        )
    }
    return json.dumps(rate_fields, sort_keys=True, separators=(",", ":"))


def _lookup_pricing_in_model_config(
    model_config: Any,
    ref_id: str,
) -> dict[str, Any] | None:
    """Resolve a pricing card for ``ref_id`` directly from ``model_config`` without re-entering ``LLM.get_model_config()``."""
    llm_cfg = getattr(model_config, "llm_config", None) or {}
    manifest = getattr(model_config, "manifest", None) or {}
    manifest_models = manifest.get("models") if isinstance(manifest, dict) else None
    if not isinstance(manifest_models, dict):
        return None

    ref_lower = ref_id.strip().lower()
    ref_prov: str | None = None
    ref_pricing_id = ref_id.strip()

    if isinstance(llm_cfg, dict):
        for k, entry in llm_cfg.items():
            if isinstance(entry, dict) and str(k).lower() == ref_lower:
                ref_prov = str(entry.get("system_provider") or entry.get("provider") or "").strip() or None
                ref_pricing_id = str(entry.get("model_id") or ref_id).strip()
                break
        else:
            for entry in llm_cfg.values():
                if isinstance(entry, dict) and str(entry.get("model_id") or "").lower() == ref_lower:
                    ref_prov = str(entry.get("system_provider") or entry.get("provider") or "").strip() or None
                    ref_pricing_id = ref_id.strip()
                    break

    def _search_cards(cards: Any, target: str) -> dict[str, Any] | None:
        if not isinstance(cards, list):
            return None
        t_low = target.lower()
        t_norm = t_low.replace(".", "-")
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = str(card.get("id") or "")
            if cid.lower() == t_low or cid.lower().replace(".", "-") == t_norm:
                pricing = card.get("pricing")
                if isinstance(pricing, dict):
                    return pricing
        for card in cards:
            if not isinstance(card, dict):
                continue
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                for alias in aliases:
                    al = str(alias).lower()
                    if al == t_low or al.replace(".", "-") == t_norm:
                        pricing = card.get("pricing")
                        if isinstance(pricing, dict):
                            return pricing
        return None

    candidate_ids = [ref_pricing_id] if ref_pricing_id == ref_id else [ref_pricing_id, ref_id]
    search_providers: list[str] = []
    if ref_prov:
        search_providers.append(ref_prov)
        get_parent = getattr(model_config, "get_parent_provider", None)
        if callable(get_parent):
            parent = get_parent(ref_prov)
            if parent and parent != ref_prov:
                search_providers.append(str(parent))

    for target in candidate_ids:
        for prov in search_providers:
            hit = _search_cards(manifest_models.get(prov), target)
            if hit is not None:
                return hit
        for prov_cards in manifest_models.values():
            hit = _search_cards(prov_cards, target)
            if hit is not None:
                return hit
    return None


def _resolve_meta_entry_notional_card(
    key: str,
    meta_entry: dict[str, Any],
    *,
    resolved_so_far: dict[str, dict[str, Any]],
    model_config: Any = None,
) -> dict[str, Any] | None:
    np = meta_entry.get("notional_pricing")
    if isinstance(np, dict):
        return normalize_notional_card(np)
    if meta_entry.get("notional_ref"):
        ref_id = str(meta_entry["notional_ref"])
        if ref_id in resolved_so_far:
            return copy.deepcopy(resolved_so_far[ref_id])
        raw_card: dict[str, Any] | None = None
        if model_config is not None:
            raw_card = _lookup_pricing_in_model_config(model_config, ref_id)
        else:
            from src.llms.pricing_utils import find_model_pricing, resolve_pricing_identity

            ref_prov, ref_pricing_id = resolve_pricing_identity(ref_id)
            raw_card = find_model_pricing(ref_pricing_id, provider=ref_prov)
            if raw_card is None and ref_pricing_id != ref_id:
                raw_card = find_model_pricing(ref_id)
        if isinstance(raw_card, dict):
            return normalize_notional_card(raw_card)
    return None


def check_notional_pricing_ambiguity(
    *,
    model_config: Any,
    dalton_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect active models sharing ``(provider.lower(), norm_id(model_id))`` with conflicting notional rates.

    Returns a list of conflict descriptors (empty when unambiguous). Used by pre-save
    validation to raise ``400 ambiguous_notional_pricing`` (§2.2 #1).
    """
    llm_cfg = getattr(model_config, "llm_config", None) or {}
    resolved_cards: dict[str, dict[str, Any]] = {}
    for key, meta_entry in dalton_meta.items():
        if not isinstance(meta_entry, dict):
            continue
        card = _resolve_meta_entry_notional_card(
            key, meta_entry, resolved_so_far=resolved_cards, model_config=model_config
        )
        if card is not None:
            resolved_cards[str(key)] = card

    by_pair: dict[tuple[str, str], list[tuple[str, str, dict[str, Any]]]] = {}
    for key, entry in llm_cfg.items():
        if not isinstance(entry, dict):
            continue
        prov = str(entry.get("provider") or "").strip().lower()
        mid = str(entry.get("model_id") or "").strip()
        if not prov or not mid:
            continue
        card = resolved_cards.get(str(key))
        if card is None:
            continue
        norm_mid = mid.lower().replace(".", "-")
        sig = _canonical_card_signature(card)
        by_pair.setdefault((prov, norm_mid), []).append((str(key), sig, card))

    conflicts: list[dict[str, Any]] = []
    for (prov, norm_mid), rows in sorted(by_pair.items()):
        unique_sigs = {sig for _, sig, _ in rows}
        if len(unique_sigs) > 1:
            conflicts.append(
                {
                    "provider": prov,
                    "normalized_model_id": norm_mid,
                    "model_keys": sorted(k for k, _, _ in rows),
                }
            )
    return conflicts


def _is_cash_bare_key(
    mid: str,
    *,
    dalton_meta: dict[str, Any],
    llm_cfg: dict[str, Any] | None = None,
) -> bool:
    """Return True when bare ``mid`` corresponds to an active cash PAYG model."""
    meta = dalton_meta.get(mid)
    if isinstance(meta, dict):
        billing = str(meta.get("billing") or "").strip().lower()
        if billing == "cash":
            return True
        if billing == "subscription":
            return False
    if isinstance(llm_cfg, dict):
        entry = llm_cfg.get(mid)
        if isinstance(entry, dict):
            prov = str(entry.get("provider") or "").strip().lower()
            if (
                not mid.endswith("-oauth")
                and prov != "z-ai-cn-coding"
                and not prov.endswith("-oauth")
            ):
                return True
    return False


def build_notional_price_index_for_catalog(
    *,
    model_config: Any,
    dalton_meta: dict[str, dict[str, Any]],
    oauth_sync_enabled: bool = True,
) -> dict[str, dict[str, Any]]:
    """Build notional price index keyed primarily by ``(provider.lower(), norm_id(model_id))``
    plus exact ``model_key`` secondary keys and unambiguous bare ``model_id`` keys (§2.2 #1)."""
    out: dict[str, dict[str, Any]] = {}
    bare_mid_candidates: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    llm_cfg = getattr(model_config, "llm_config", None) or {}

    def _record_bare_mid(mid: str, card: dict[str, Any]) -> None:
        cleaned = mid.strip()
        if not cleaned or _is_cash_bare_key(
            cleaned, dalton_meta=dalton_meta, llm_cfg=llm_cfg
        ):
            return
        norm_mid = cleaned.lower().replace(".", "-")
        bare_mid_candidates.setdefault(norm_mid, []).append(
            (cleaned, _canonical_card_signature(card), card)
        )

    for key, meta_entry in dalton_meta.items():
        if not isinstance(meta_entry, dict):
            continue
        card = _resolve_meta_entry_notional_card(
            str(key), meta_entry, resolved_so_far=out, model_config=model_config
        )
        if card is None:
            continue
        out[str(key)] = card
        m_entry = llm_cfg.get(str(key))
        if isinstance(m_entry, dict):
            prov = str(m_entry.get("provider") or "").strip()
            mid = str(m_entry.get("model_id") or "").strip()
            if prov and mid:
                out[notional_pair_key(prov, mid)] = card
            if mid:
                _record_bare_mid(mid, card)

    if oauth_sync_enabled:
        for k, fallback_card in _FALLBACK_OAUTH_CARDS.items():
            already_present = k in out
            fb_card = dict(fallback_card)
            out.setdefault(k, fb_card)
            m_entry = llm_cfg.get(k)
            if isinstance(m_entry, dict):
                prov = str(m_entry.get("provider") or "").strip()
                mid = str(m_entry.get("model_id") or "").strip()
                if prov and mid:
                    out.setdefault(notional_pair_key(prov, mid), dict(fallback_card))
                if mid and not already_present:
                    _record_bare_mid(mid, fb_card)

    for rows in bare_mid_candidates.values():
        unique_sigs = {sig for _, sig, _ in rows}
        if len(unique_sigs) == 1:
            for exact_mid, _, card in rows:
                out.setdefault(exact_mid, card)
    return out


def build_notional_price_index(
    overlay_loader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build notional price index with ``(provider, norm_id(model_id))`` primary keys, ``model_key`` and unambiguous bare ``model_id`` fallback."""
    if (
        overlay_loader is None
        and _overlay_mod.load_dalton_meta is _DEFAULT_LOAD_DALTON_META
        and not _overlay_mod._has_disk_overlay_test_override()
    ):
        try:
            from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_sync

            compiled = get_compiled_catalog_sync()
            if compiled is not None and isinstance(compiled.notional_price_index, dict):
                return copy.deepcopy(compiled.notional_price_index)
        except Exception:
            pass

    loader = overlay_loader or _overlay_mod.load_overlay_data
    out: dict[str, dict[str, Any]] = {}
    bare_mid_candidates: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}

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
                    mid = str(m_entry["model_id"]).strip()
                    prov = str(m_entry.get("provider") or "").strip()
                    if prov and mid:
                        out[notional_pair_key(prov, mid)] = card
                    if mid and not _is_cash_bare_key(
                        mid,
                        dalton_meta=dalton_meta,
                        llm_cfg=native_models if isinstance(native_models, dict) else None,
                    ):
                        norm_mid = mid.lower().replace(".", "-")
                        bare_mid_candidates.setdefault(norm_mid, []).append(
                            (mid, _canonical_card_signature(card), card)
                        )
    except Exception as exc:
        logger.debug("[DaltonNotional] build_notional_price_index dalton_meta lookup skipped: %s", exc)

    for rows in bare_mid_candidates.values():
        unique_sigs = {sig for _, sig, _ in rows}
        if len(unique_sigs) == 1:
            for exact_mid, _, card in rows:
                out.setdefault(exact_mid, card)

    for k, fallback_card in _FALLBACK_OAUTH_CARDS.items():
        out.setdefault(k, dict(fallback_card))
    return out


def is_subscription_model(
    model_name: str,
    billing_type: str = "",
    *,
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
) -> bool:
    """Return True when the model or route uses subscription/OAuth billing."""
    if billing_type == "oauth" or model_name.endswith("-oauth"):
        return True
    if pricing_model_id and pricing_model_id.endswith("-oauth"):
        return True
    if pricing_provider:
        if pricing_provider == "z-ai-cn-coding" or pricing_provider.endswith("-oauth"):
            return True
        from src.llms.pricing_utils import is_subscription_provider

        if is_subscription_provider(provider=pricing_provider):
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

    return False


def resolve_notional_card(
    model_name: str,
    *,
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
    index: dict[str, dict[str, Any]] | None = None,
    overlay_loader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve the normalized nominal pricing card by ``(provider, norm_id(model_id))`` or ``model_key``.

    Checks the active turn's ``effective_catalog_snapshot`` first when bound (§2.4).
    """
    if index is None and overlay_loader is None:
        from src.dalton.services.team_model_catalog.snapshot import lookup_snapshot_notional

        snap_hit, snap_card, _ = lookup_snapshot_notional(
            model_name,
            pricing_model_id=pricing_model_id,
            pricing_provider=pricing_provider,
        )
        if snap_hit:
            return snap_card

    idx = index if index is not None else build_notional_price_index(overlay_loader=overlay_loader)
    if pricing_model_id and pricing_provider:
        # Primary lookup: exact normalized (provider, norm_id(model_id)) pair key (§2.2 #1)
        pkey = notional_pair_key(pricing_provider, pricing_model_id)
        if pkey in idx:
            return copy.deepcopy(idx[pkey])
        if model_name and model_name in idx:
            return copy.deepcopy(idx[model_name])
        from src.llms.pricing_utils import resolve_pricing_identity

        want_prov = pricing_provider.strip().lower()
        want_norm = pricing_model_id.strip().lower().replace(".", "-")
        for candidate, card in idx.items():
            if "\0" in candidate:
                continue
            c_prov, c_id = resolve_pricing_identity(candidate, billing_type="byok")
            if (
                c_prov
                and c_prov.strip().lower() == want_prov
                and c_id.strip().lower().replace(".", "-") == want_norm
            ):
                return copy.deepcopy(card)
        return None
    identity = pricing_model_id or model_name
    for candidate in (
        identity,
        f"{identity}-oauth" if identity and not identity.endswith("-oauth") else None,
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
