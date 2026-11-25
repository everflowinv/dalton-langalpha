"""Turn-scoped & cross-deploy ``effective_catalog_snapshot`` persistence and lookup.

At turn START (``conversation_responses.metadata``) and subagent START
(``subagent_runs.replay_facts``), the server persists a self-contained snapshot
of the candidate models for that run:
- Cash pricing card (``pricing``)
- Nominal subscription pricing card (``notional_card``) and ``billing``
- ``input_modalities``, ``context``, ``max_output``, ``compaction_profile``
- ``reasoning`` ladder/default/write surface
- Fixed route (``provider``, ``model_id``)
- Non-secret catalog variant binding identifiers (``bound_base_url``,
  ``canonical_bound_url``, ``key_fingerprint``)

In-flight calls, settlement (``UsagePersistence`` / ``calculate_cost_from_per_call_records``),
``MultimodalStripMiddleware``, and cross-deploy restart recovery (``restart.py``)
all read from the bound snapshot so rolling deploys or mid-turn price edits never
shift an in-flight turn's rates, capabilities, or credential binding check.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.constants import (
    TURN_SNAPSHOT_SCHEMA,
    norm_id,
)

logger = logging.getLogger(__name__)

_ACTIVE_CATALOG_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar(
    "dalton_active_catalog_snapshot", default=None
)


def get_active_catalog_snapshot() -> dict[str, Any] | None:
    """Return the ``effective_catalog_snapshot`` bound to the current async/thread context."""
    return _ACTIVE_CATALOG_SNAPSHOT.get()


@contextmanager
def bind_catalog_snapshot(snapshot: dict[str, Any] | None) -> Iterator[dict[str, Any] | None]:
    """Bind ``snapshot`` to the current execution context for the duration of the block."""
    token = _ACTIVE_CATALOG_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _ACTIVE_CATALOG_SNAPSHOT.reset(token)


def _identity_key(provider: str | None, model_id: str | None) -> str:
    return f"{str(provider or '').strip().lower()}\0{norm_id(model_id)}"


def _extract_cash_pricing_card(
    mc: Any,
    provider: str,
    model_id: str,
) -> dict[str, Any] | None:
    """Resolve the cash pricing card for ``(provider, model_id)`` from ``mc.manifest``."""
    manifest_models = (getattr(mc, "manifest", None) or {}).get("models") or {}
    if not isinstance(manifest_models, Mapping):
        return None
    target_norm = norm_id(model_id)
    search_providers: list[str] = []
    if provider:
        search_providers.append(provider)
        parent = mc.get_parent_provider(provider)
        if parent and parent != provider:
            search_providers.append(parent)
    for prov in search_providers:
        cards = manifest_models.get(prov)
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            cid = str(card.get("id") or "")
            if cid.lower() == model_id.lower() or norm_id(cid) == target_norm:
                pricing = card.get("pricing")
                return copy.deepcopy(dict(pricing)) if isinstance(pricing, Mapping) else None
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                for alias in aliases:
                    if str(alias).lower() == model_id.lower() or norm_id(str(alias)) == target_norm:
                        pricing = card.get("pricing")
                        return (
                            copy.deepcopy(dict(pricing)) if isinstance(pricing, Mapping) else None
                        )
    return None


def build_effective_catalog_snapshot(
    model_keys: Iterable[str],
    *,
    compiled: Any = None,
    variant_credentials: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a self-contained ``effective_catalog_snapshot`` dict for ``model_keys``."""
    from src.dalton.llm.notional_pricing import resolve_notional_card
    from src.dalton.llm.overlay import _is_oauth_model_key
    from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_sync

    cat = compiled if compiled is not None else get_compiled_catalog_sync()
    mc = cat.model_config
    llm_cfg = mc.llm_config or {}
    meta_map = cat.dalton_meta or {}

    creds_map: Mapping[str, Mapping[str, Any]] = variant_credentials or {}
    if not creds_map and cat.provider_variants:
        try:
            from src.dalton.database.team_model_catalog import (
                list_catalog_provider_credentials_sync,
            )

            creds_map = list_catalog_provider_credentials_sync()
        except Exception:
            creds_map = {}

    models_out: dict[str, dict[str, Any]] = {}
    by_identity: dict[str, dict[str, Any]] = {}
    variants_out: dict[str, dict[str, Any]] = {}

    unique_keys = [str(k).strip() for k in model_keys if str(k or "").strip()]
    for key in dict.fromkeys(unique_keys):
        entry = llm_cfg.get(key)
        if not isinstance(entry, Mapping):
            continue
        provider = str(entry.get("provider") or "").strip()
        model_id = str(entry.get("model_id") or key).strip()
        prov_info = mc.get_provider_info(provider) if provider else {}
        is_catalog_variant = bool(prov_info.get("catalog_defined"))

        meta_entry = meta_map.get(key) if isinstance(meta_map.get(key), Mapping) else {}
        billing = str(
            (meta_entry or {}).get("billing")
            or prov_info.get("billing")
            or (
                "subscription"
                if _is_oauth_model_key(provider.lower()) or provider == "z-ai-cn-coding"
                else "cash"
            )
        )
        cash_card = _extract_cash_pricing_card(mc, provider, model_id)
        notional_card = resolve_notional_card(
            key,
            pricing_model_id=model_id,
            pricing_provider=provider,
            index=cat.notional_price_index,
        )

        modalities = entry.get("input_modalities")
        if not isinstance(modalities, list) or not modalities:
            modalities = ["text"]

        params = entry.get("parameters") if isinstance(entry.get("parameters"), Mapping) else {}
        max_output = params.get("max_tokens") or params.get("max_output_tokens")

        bound_base_url: str | None = None
        canonical_bound: str | None = None
        key_fingerprint: str | None = None
        if is_catalog_variant:
            cred = creds_map.get(provider)
            raw_url = (cred or {}).get("bound_base_url") or prov_info.get("base_url")
            if isinstance(raw_url, str) and raw_url.strip():
                bound_base_url = raw_url.strip()
                canonical_bound = (cred or {}).get("canonical_bound_url") or canonical_url_key(
                    bound_base_url
                )
            if cred and cred.get("key_fingerprint"):
                key_fingerprint = str(cred["key_fingerprint"])
            variants_out[provider] = {
                "provider_slug": provider,
                "bound_base_url": bound_base_url,
                "canonical_bound_url": canonical_bound,
                "key_fingerprint": key_fingerprint,
            }

        snap_entry: dict[str, Any] = {
            "model_key": key,
            "provider": provider,
            "model_id": model_id,
            "billing": billing,
            "pricing": cash_card,
            "notional_card": notional_card,
            "input_modalities": list(modalities),
            "context": entry.get("context"),
            "max_output": max_output,
            "compaction_profile": entry.get("compaction_profile"),
            "reasoning": copy.deepcopy(entry.get("reasoning"))
            if isinstance(entry.get("reasoning"), Mapping)
            else None,
            "catalog_defined": is_catalog_variant,
        }
        if is_catalog_variant:
            snap_entry["bound_base_url"] = bound_base_url
            snap_entry["canonical_bound_url"] = canonical_bound
            snap_entry["key_fingerprint"] = key_fingerprint

        models_out[key] = snap_entry
        if provider and model_id:
            by_identity[_identity_key(provider, model_id)] = {
                "model_key": key,
                "provider": provider,
                "model_id": model_id,
                "billing": billing,
                "pricing": cash_card,
                "notional_card": notional_card,
            }

    return {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "catalog_revision": int(cat.revision),
        "models": models_out,
        "by_identity": by_identity,
        "variants": variants_out,
    }


def _find_snapshot_entry(
    snapshot: Mapping[str, Any] | None,
    *,
    model_name: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
) -> Mapping[str, Any] | None:
    if not isinstance(snapshot, Mapping):
        return None
    by_identity = snapshot.get("by_identity")
    if provider and (model_id or model_name) and isinstance(by_identity, Mapping):
        hit = by_identity.get(_identity_key(provider, model_id or model_name))
        if isinstance(hit, Mapping):
            return hit
    models = snapshot.get("models")
    if isinstance(models, Mapping):
        if model_name and isinstance(models.get(model_name), Mapping):
            return models[model_name]
        if model_id and isinstance(models.get(model_id), Mapping):
            return models[model_id]
        if provider and (model_id or model_name):
            want_prov = provider.strip().lower()
            want_norm = norm_id(model_id or model_name)
            for item in models.values():
                if not isinstance(item, Mapping):
                    continue
                if (
                    str(item.get("provider") or "").strip().lower() == want_prov
                    and norm_id(str(item.get("model_id") or "")) == want_norm
                ):
                    return item
    return None


def lookup_snapshot_pricing(
    model_name: str,
    provider: str | None = None,
    *,
    model_id: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Return ``(hit_in_snapshot, pricing_card_or_subscription_marker)``."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(snap, model_name=model_name, provider=provider, model_id=model_id)
    if entry is None:
        return False, None
    if str(entry.get("billing") or "") == "subscription" and not entry.get("pricing"):
        return True, {"pricing_type": "subscription"}
    pricing = entry.get("pricing")
    return True, (copy.deepcopy(dict(pricing)) if isinstance(pricing, Mapping) else None)


def lookup_snapshot_notional(
    model_name: str,
    *,
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None, bool]:
    """Return ``(hit_in_snapshot, notional_card, is_subscription)``."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(
        snap,
        model_name=model_name,
        provider=pricing_provider,
        model_id=pricing_model_id,
    )
    if entry is None:
        return False, None, False
    is_sub = str(entry.get("billing") or "") == "subscription"
    card = entry.get("notional_card")
    return True, (copy.deepcopy(dict(card)) if isinstance(card, Mapping) else None), is_sub


def lookup_snapshot_modalities(
    model_name: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> list[str] | None:
    """Return snapshotted ``input_modalities`` for ``model_name``, or ``None`` if absent."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(snap, model_name=model_name)
    if entry is None:
        return None
    mods = entry.get("input_modalities")
    if isinstance(mods, list) and mods:
        return [str(m) for m in mods]
    return None


def lookup_snapshot_variant_binding(
    provider_slug: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return ``{bound_base_url, canonical_bound_url, key_fingerprint}`` from active snapshot."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    if not isinstance(snap, Mapping):
        return None
    variants = snap.get("variants")
    if isinstance(variants, Mapping):
        hit = variants.get(provider_slug)
        if isinstance(hit, Mapping):
            return dict(hit)
    models = snap.get("models")
    if isinstance(models, Mapping):
        for entry in models.values():
            if (
                isinstance(entry, Mapping)
                and entry.get("provider") == provider_slug
                and entry.get("catalog_defined")
            ):
                return {
                    "provider_slug": provider_slug,
                    "bound_base_url": entry.get("bound_base_url"),
                    "canonical_bound_url": entry.get("canonical_bound_url"),
                    "key_fingerprint": entry.get("key_fingerprint"),
                }
    return None


def _extract_candidate_models_from_run(
    run_row: Mapping[str, Any],
    extra_model_keys: Iterable[str] | None = None,
) -> list[str]:
    keys: list[str] = []
    for k in extra_model_keys or ():
        if isinstance(k, str) and k.strip():
            keys.append(k.strip())
    for container_key in ("metadata", "replay_facts"):
        container = run_row.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for field_name in ("model", "llm_model", "model_override", "main_model"):
            val = container.get(field_name)
            if isinstance(val, str) and val.strip():
                keys.append(val.strip())
        fallbacks = container.get("fallback_models")
        if isinstance(fallbacks, list):
            for fb in fallbacks:
                if isinstance(fb, str) and fb.strip():
                    keys.append(fb.strip())
    for field_name in ("model", "llm_model"):
        val = run_row.get(field_name)
        if isinstance(val, str) and val.strip():
            keys.append(val.strip())
    return list(dict.fromkeys(keys))


async def ensure_run_catalog_snapshot(
    run_row: Mapping[str, Any] | None,
    *,
    model_keys: Iterable[str] | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return persisted ``effective_catalog_snapshot`` or conditionally backfill it for legacy runs.

    Per §2.4 #3 (first-launch upgrade compatibility):
    - If the run row already carries ``effective_catalog_snapshot``, return it verbatim.
    - If missing (pre-feature run), extract candidate models from the current baseline
      catalog and write it conditionally (only when absent in DB).
    - If candidate models no longer exist or metadata is incomplete so no snapshot can
      be built, log a structured message and return ``None`` so callers gracefully fall
      back to pre-feature finish/settlement behavior.
    """
    if not isinstance(run_row, Mapping):
        return None
    container_key = "replay_facts" if is_subagent else "metadata"
    container = run_row.get(container_key)
    if isinstance(container, Mapping):
        existing = container.get("effective_catalog_snapshot")
        if isinstance(existing, Mapping) and existing.get("schema") == TURN_SNAPSHOT_SCHEMA:
            return dict(existing)

    candidates = _extract_candidate_models_from_run(run_row, model_keys)
    if not candidates:
        logger.info(
            "[team_model_catalog] Legacy run missing effective_catalog_snapshot and model metadata; "
            "falling back to pre-feature handling"
        )
        return None

    from src.dalton.database.team_model_catalog import (
        backfill_run_catalog_snapshot,
        list_catalog_provider_credentials,
    )
    from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_async

    compiled = await get_compiled_catalog_async(conn=conn)
    try:
        creds = await list_catalog_provider_credentials(conn=conn)
    except Exception:
        creds = {}

    snapshot = build_effective_catalog_snapshot(
        candidates,
        compiled=compiled,
        variant_credentials=creds,
    )
    if not snapshot.get("models"):
        logger.info(
            "[team_model_catalog] Legacy run models %s missing from current catalog; "
            "cannot backfill snapshot, falling back to pre-feature handling",
            candidates,
        )
        return None

    run_id = str(
        run_row.get("task_run_id" if is_subagent else "conversation_response_id")
        or run_row.get("run_id")
        or ""
    ).strip()
    if run_id:
        try:
            await backfill_run_catalog_snapshot(
                run_id,
                snapshot,
                is_subagent=is_subagent,
                conn=conn,
            )
        except Exception as exc:
            logger.warning(
                "[team_model_catalog] Conditional snapshot backfill failed for run=%s: %s",
                run_id,
                type(exc).__name__,
            )
    return snapshot
