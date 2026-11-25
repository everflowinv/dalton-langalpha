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
import json
import logging
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import aclosing, contextmanager, suppress
from contextvars import ContextVar
from typing import Any

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.constants import (
    TURN_SNAPSHOT_SCHEMA,
    norm_id,
    normalize_cash_pricing_card,
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


async def run_with_catalog_snapshot(
    snapshot: dict[str, Any] | None,
    stream: AsyncIterator[Any],
) -> AsyncIterator[Any]:
    """Drive a turn's async stream with ``effective_catalog_snapshot`` bound in its task context."""
    if snapshot is None:
        async with aclosing(stream):
            async for event in stream:
                yield event
        return
    token = _ACTIVE_CATALOG_SNAPSHOT.set(snapshot)
    try:
        async with aclosing(stream):
            async for event in stream:
                yield event
    finally:
        with suppress(ValueError):
            _ACTIVE_CATALOG_SNAPSHOT.reset(token)


async def load_run_catalog_snapshot(
    run_id: str,
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Authoritatively load (or backfill) ``effective_catalog_snapshot`` from a persisted run row."""
    cleaned_id = str(run_id or "").strip()
    if not cleaned_id:
        return None
    from psycopg.rows import dict_row

    from src.server.database.pool import get_db_connection

    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            if is_subagent:
                await cur.execute(
                    """
                    SELECT task_run_id::text AS task_run_id, thread_id, replay_facts
                    FROM subagent_runs
                    WHERE task_run_id = %s::uuid
                    """,
                    (cleaned_id,),
                )
            elif thread_id:
                await cur.execute(
                    """
                    SELECT conversation_response_id::text AS conversation_response_id,
                           conversation_thread_id, metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s::uuid
                      AND conversation_thread_id = %s
                    """,
                    (cleaned_id, str(thread_id)),
                )
            else:
                await cur.execute(
                    """
                    SELECT conversation_response_id::text AS conversation_response_id,
                           conversation_thread_id, metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s::uuid
                    """,
                    (cleaned_id,),
                )
            row = await cur.fetchone()
    if not row:
        return None
    return await ensure_run_catalog_snapshot(dict(row), is_subagent=is_subagent, conn=conn)


_SNAPSHOT_PROVIDER_INFO_KEYS = (
    "sdk",
    "access_type",
    "billing",
    "base_url",
    "use_response_api",
    "use_previous_response_id",
    "prompt_cache_key",
    "default_headers",
    "session_header",
    "parent_provider",
    "catalog_defined",
    "own_api_key",
    "env_key",
)


def serialize_catalog_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Serialize ``snapshot`` with the shared PG-safe JSON encoder (identical across first-write and backfill)."""
    from src.server.utils.pg_sanitize import _safe_dumps

    return _safe_dumps(snapshot)


def _identity_key(provider: str | None, model_id: str | None) -> str:
    """Lossless JSON-safe identity key for ``(provider.lower(), norm_id(model_id))`` (no NUL bytes)."""
    return json.dumps(
        [str(provider or "").strip().lower(), norm_id(model_id)],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _legacy_nul_identity_key(provider: str | None, model_id: str | None) -> str:
    return f"{str(provider or '').strip().lower()}\0{norm_id(model_id)}"


def _legacy_stripped_identity_key(provider: str | None, model_id: str | None) -> str:
    return f"{str(provider or '').strip().lower()}{norm_id(model_id)}"


def _snapshot_provider_info_subset(
    prov_info: Mapping[str, Any] | None,
    *,
    is_catalog_variant: bool,
    bound_base_url: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(prov_info, Mapping):
        for k in _SNAPSHOT_PROVIDER_INFO_KEYS:
            if is_catalog_variant and k == "env_key":
                continue
            val = prov_info.get(k)
            if val is not None:
                out[k] = copy.deepcopy(val)
    if is_catalog_variant:
        out["catalog_defined"] = True
        out["own_api_key"] = True
        out.pop("env_key", None)
        out.pop("models_url", None)
        out.pop("platform", None)
        if bound_base_url:
            out["base_url"] = bound_base_url
    return out


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
                return normalize_cash_pricing_card(pricing) if isinstance(pricing, Mapping) else None
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                for alias in aliases:
                    if str(alias).lower() == model_id.lower() or norm_id(str(alias)) == target_norm:
                        pricing = card.get("pricing")
                        return (
                            normalize_cash_pricing_card(pricing)
                            if isinstance(pricing, Mapping)
                            else None
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
        is_sub = (
            str(prov_info.get("billing") or "").strip().lower() == "subscription"
            or str((meta_entry or {}).get("billing") or "").strip().lower() == "subscription"
            or _is_oauth_model_key(provider.lower())
            or provider == "z-ai-cn-coding"
        )
        billing = "subscription" if is_sub else "cash"
        cash_card = (
            None
            if billing == "subscription"
            else _extract_cash_pricing_card(mc, provider, model_id)
        )
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
        extra_body = (
            entry.get("extra_body") if isinstance(entry.get("extra_body"), Mapping) else {}
        )
        additional_betas = (
            list(entry.get("additional_betas"))
            if isinstance(entry.get("additional_betas"), list)
            else []
        )
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

        snap_prov_info = _snapshot_provider_info_subset(
            prov_info,
            is_catalog_variant=is_catalog_variant,
            bound_base_url=bound_base_url,
        )
        if is_catalog_variant:
            variants_out[provider] = {
                "provider_slug": provider,
                "bound_base_url": bound_base_url,
                "canonical_bound_url": canonical_bound,
                "key_fingerprint": key_fingerprint,
                "provider_info": copy.deepcopy(snap_prov_info),
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
            "parameters": copy.deepcopy(dict(params)),
            "extra_body": copy.deepcopy(dict(extra_body)),
            "reasoning": copy.deepcopy(entry.get("reasoning"))
            if isinstance(entry.get("reasoning"), Mapping)
            else None,
            "catalog_defined": is_catalog_variant,
            "provider_info": snap_prov_info,
        }
        if additional_betas:
            snap_entry["additional_betas"] = additional_betas
        if entry.get("system_provider") is not None:
            snap_entry["system_provider"] = entry.get("system_provider")
        if entry.get("use_response_api") is not None:
            snap_entry["use_response_api"] = entry.get("use_response_api")
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


def _entry_matches_request(
    entry: Mapping[str, Any],
    *,
    provider: str | None = None,
    model_id: str | None = None,
) -> bool:
    """Verify ``entry`` matches the actual requested ``provider`` and ``model_id`` when specified."""
    if provider:
        entry_prov = str(entry.get("provider") or "").strip().lower()
        if entry_prov != provider.strip().lower():
            return False
    if model_id:
        entry_mid = norm_id(str(entry.get("model_id") or ""))
        if entry_mid != norm_id(model_id):
            return False
    return True


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
    want_id = model_id or model_name
    if provider and want_id and isinstance(by_identity, Mapping):
        for key_candidate in (
            _identity_key(provider, want_id),
            _legacy_nul_identity_key(provider, want_id),
            _legacy_stripped_identity_key(provider, want_id),
        ):
            hit = by_identity.get(key_candidate)
            if isinstance(hit, Mapping) and _entry_matches_request(
                hit, provider=provider, model_id=want_id
            ):
                return hit
    models = snapshot.get("models")
    if isinstance(models, Mapping):
        if model_name and isinstance(models.get(model_name), Mapping):
            cand = models[model_name]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
        if model_id and isinstance(models.get(model_id), Mapping):
            cand = models[model_id]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
        if provider and want_id:
            for item in models.values():
                if isinstance(item, Mapping) and _entry_matches_request(
                    item, provider=provider, model_id=want_id
                ):
                    return item
    return None


def lookup_snapshot_model_entry(
    model_key: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the persisted model snapshot entry for ``model_key``, or ``None`` if absent."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    if not isinstance(snap, Mapping):
        return None
    models = snap.get("models")
    if isinstance(models, Mapping):
        hit = models.get(model_key)
        if isinstance(hit, Mapping):
            return copy.deepcopy(dict(hit))
    return None


class SnapshotModelConfig:
    """Read-only ``ModelConfig`` overlay honoring persisted turn/resume ``effective_catalog_snapshot``.

    Ensures client construction, credential-route selection, and multimodal capability
    checks during an active or resumed turn follow the snapshotted provider, model_id,
    request parameters, and catalog variant metadata rather than drifting to live catalog
    edits. Never mutates the underlying process-wide ``ModelConfig``.
    """

    def __init__(self, base: Any, snapshot: Mapping[str, Any]) -> None:
        self._base = base
        self._snapshot = snapshot

    def _snap_model_entry(self, model_name: str) -> dict[str, Any] | None:
        models = self._snapshot.get("models")
        if not isinstance(models, Mapping):
            return None
        raw = models.get(model_name)
        if not isinstance(raw, Mapping):
            return None
        snap_provider = str(raw.get("provider") or "").strip()
        snap_model_id = str(raw.get("model_id") or model_name).strip()
        live_entry = self._base.get_model_config(model_name)
        same_live_route = (
            isinstance(live_entry, Mapping)
            and str(live_entry.get("provider") or "").strip() == snap_provider
        )
        out: dict[str, Any] = copy.deepcopy(dict(live_entry)) if same_live_route else {}
        out["provider"] = snap_provider
        out["model_id"] = snap_model_id
        if "parameters" in raw and isinstance(raw.get("parameters"), Mapping):
            out["parameters"] = copy.deepcopy(dict(raw["parameters"]))
        elif not same_live_route:
            out["parameters"] = {}
        if "extra_body" in raw and isinstance(raw.get("extra_body"), Mapping):
            out["extra_body"] = copy.deepcopy(dict(raw["extra_body"]))
        elif not same_live_route:
            out["extra_body"] = {}
        if "additional_betas" in raw and isinstance(raw.get("additional_betas"), list):
            out["additional_betas"] = list(raw["additional_betas"])
        elif not same_live_route:
            out.pop("additional_betas", None)
        if "system_provider" in raw:
            out["system_provider"] = raw.get("system_provider")
        elif not same_live_route:
            out.pop("system_provider", None)
        if "use_response_api" in raw:
            out["use_response_api"] = raw.get("use_response_api")
        elif not same_live_route:
            out.pop("use_response_api", None)
        if raw.get("reasoning") is not None:
            out["reasoning"] = copy.deepcopy(raw["reasoning"])
        elif "reasoning" in raw:
            out.pop("reasoning", None)
        if raw.get("context") is not None:
            out["context"] = raw["context"]
        if isinstance(raw.get("input_modalities"), list) and raw["input_modalities"]:
            out["input_modalities"] = list(raw["input_modalities"])
        if raw.get("compaction_profile") is not None:
            out["compaction_profile"] = raw["compaction_profile"]
        return out

    def _snap_provider_overlay(self, provider: str) -> dict[str, Any] | None:
        prov = str(provider or "").strip()
        if not prov:
            return None
        variants = self._snapshot.get("variants")
        if isinstance(variants, Mapping):
            v_entry = variants.get(prov)
            if isinstance(v_entry, Mapping):
                pinfo = (
                    copy.deepcopy(dict(v_entry["provider_info"]))
                    if isinstance(v_entry.get("provider_info"), Mapping)
                    else {}
                )
                pinfo["catalog_defined"] = True
                pinfo["own_api_key"] = True
                pinfo.pop("env_key", None)
                pinfo.pop("models_url", None)
                pinfo.pop("platform", None)
                if v_entry.get("bound_base_url"):
                    pinfo["base_url"] = str(v_entry["bound_base_url"])
                return pinfo
        models = self._snapshot.get("models")
        if isinstance(models, Mapping):
            for m_entry in models.values():
                if not isinstance(m_entry, Mapping):
                    continue
                if str(m_entry.get("provider") or "").strip() != prov:
                    continue
                pinfo = (
                    copy.deepcopy(dict(m_entry["provider_info"]))
                    if isinstance(m_entry.get("provider_info"), Mapping)
                    else {}
                )
                if m_entry.get("catalog_defined"):
                    pinfo["catalog_defined"] = True
                    pinfo["own_api_key"] = True
                    pinfo.pop("env_key", None)
                    pinfo.pop("models_url", None)
                    pinfo.pop("platform", None)
                    if m_entry.get("bound_base_url"):
                        pinfo["base_url"] = str(m_entry["bound_base_url"])
                return pinfo or None
        return None

    def get_model_config(self, model_id: str) -> dict[str, Any] | None:
        snap_entry = self._snap_model_entry(model_id)
        if snap_entry is not None:
            return snap_entry
        return self._base.get_model_config(model_id)

    def get_provider_info(self, provider: str) -> dict[str, Any]:
        live_info = self._base.get_provider_info(provider)
        overlay = self._snap_provider_overlay(provider)
        if overlay is None:
            return live_info
        merged = copy.deepcopy(dict(live_info)) if isinstance(live_info, Mapping) else {}
        merged.update(overlay)
        if overlay.get("catalog_defined"):
            merged.pop("env_key", None)
            merged.pop("models_url", None)
            merged.pop("platform", None)
        return merged

    def get_parent_provider(self, provider: str) -> str:
        info = self.get_provider_info(provider)
        return str(info.get("parent_provider", provider))

    def get_input_modalities(self, model_name: str) -> list[str]:
        entry = self.get_model_config(model_name)
        if not entry:
            return ["text"]
        return list(entry.get("input_modalities") or ["text"])

    @property
    def llm_config(self) -> dict[str, Any]:
        merged = dict(getattr(self._base, "llm_config", None) or {})
        models = self._snapshot.get("models")
        if isinstance(models, Mapping):
            for key in models:
                snap_entry = self._snap_model_entry(str(key))
                if snap_entry is not None:
                    merged[str(key)] = snap_entry
        return merged

    @property
    def flat_providers(self) -> dict[str, Any]:
        merged = dict(getattr(self._base, "flat_providers", None) or {})
        seen: set[str] = set()
        variants = self._snapshot.get("variants")
        if isinstance(variants, Mapping):
            seen.update(str(k) for k in variants)
        models = self._snapshot.get("models")
        if isinstance(models, Mapping):
            for item in models.values():
                if isinstance(item, Mapping) and item.get("provider"):
                    seen.add(str(item["provider"]))
        for prov in seen:
            info = self.get_provider_info(prov)
            if info:
                merged[prov] = info
        return merged

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def overlay_model_config_with_snapshot(
    live_mc: Any,
    snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Return a snapshot-overlaid ``ModelConfig`` view when an active snapshot is bound."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    if not isinstance(snap, Mapping) or not isinstance(snap.get("models"), Mapping) or not snap["models"]:
        return live_mc
    if isinstance(live_mc, SnapshotModelConfig):
        if live_mc._snapshot is snap:
            return live_mc
        live_mc = live_mc._base
    return SnapshotModelConfig(live_mc, snap)


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
    if str(entry.get("billing") or "").strip().lower() == "subscription":
        return True, {"pricing_type": "subscription"}
    pricing = entry.get("pricing")
    return True, (normalize_cash_pricing_card(pricing) if isinstance(pricing, Mapping) else None)


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
