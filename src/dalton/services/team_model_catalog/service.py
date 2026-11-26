"""Team Model Catalog service: CRUD, 6-source retire, chained rollback, and test-call.

Implements §2.7, §3.1–§3.4, §5.4, and Batch 1 acceptance scenarios:
- Admin list/detail/create/update/hide/unhide/retire/restore/restore-factory-default
- Provider variant upsert with write-only encrypted credentials & preflight URL guard
- Curated setup update & version-aligned ``dalton_setup_candidates`` derivation
- Outermost catalog shared-lock model check for all model-reference write entry points
- 4-step rollback with full ``base_url`` credential check, atomic chained ``A -> B -> C``
  task restoration (preserving user-modified tasks), and post-restore reference checks
- Rate-limited, 15s-bounded ``test-call`` diagnostic endpoint
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.database.team_model_catalog import (
    acquire_catalog_exclusive_lock,
    get_catalog_revision,
    get_catalog_row,
    list_catalog_provider_credentials,
    list_catalog_revisions,
    list_revisions_between,
    save_catalog_revision,
    upsert_catalog_provider_credential,
)
from src.dalton.llm.egress_policy import canonical_url_key, endpoint_matches_prefix
from src.dalton.services import model_references
from src.dalton.services.model_references import ReferenceSourceUnavailable
from src.dalton.services.team_model_catalog.compiled import (
    CompiledCatalog,
    cache_compiled_catalog,
    compile_catalog_from_payload,
    get_compiled_catalog_async,
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_SCHEMA,
    CatalogConflictError,
    CatalogValidationError,
    empty_catalog_payload,
    norm_id,
    normalize_cash_pricing_card,
)
from src.dalton.services.team_model_catalog.follow import (
    invalidate_catalog_redis_caches,
    publish_catalog_changed,
)
from src.dalton.services.team_model_catalog.transport import (
    make_catalog_variant_transport_factory,
    preflight_catalog_variant_url,
)
from src.dalton.services.team_model_catalog.validation import (
    _SUPPORTED_VARIANT_SDKS,
    CatalogModelUpsertInput,
    CatalogRollbackInput,
    CatalogTestCallInput,
    CuratedSetupUpdateInput,
    InlineProviderVariantUpdateInput,
    ModelRestoreInput,
    ModelRetireInput,
    ModelVisibilityInput,
    ProviderVariantUpsertInput,
    trial_compile_and_validate_catalog,
    validate_model_fields,
    validate_provider_variant_input,
)
from src.server.database.pool import get_db_connection
from src.server.utils.egress_guard import EgressBlockedError
from src.utils.cache.throttle import throttle

logger = logging.getLogger(__name__)

def get_default_curated_setup_candidates() -> tuple[str, ...]:
    """Load default curated setup candidates from ``native_overlay.json`` (aligned with ``daltonSetup.ts``)."""
    from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_overlay_data

    native = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
    raw = native.get("curated_setup_defaults")
    if isinstance(raw, list):
        return tuple(str(k).strip() for k in raw if isinstance(k, str) and str(k).strip())
    return ()

_USER_PREF_SLOT_KEYS = (
    "preferred_model",
    "flash_model",
    "compaction_model",
    "fetch_model",
    "summarization_model",
)

# Strict read-only parameter whitelist for GET /api/v1/dalton/team-models:
# exposes ONLY the parameter keys rendered/edited by TeamModelEditSheet so
# non-UI or sensitive parameters are never read by or echoed from the browser,
# and remain preserved server-side across model updates.
_OVERVIEW_ALLOWED_PARAMETER_KEYS = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
    }
)


def _sanitize_public_parameters(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key in _OVERVIEW_ALLOWED_PARAMETER_KEYS:
        if key not in raw:
            continue
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            cleaned[key] = val
    return cleaned if cleaned else None


def _contains_oauth_identifier(value: Any) -> bool:
    from src.dalton.llm.overlay import _is_oauth_model_key

    if isinstance(value, str):
        return _is_oauth_model_key(value.strip().lower())
    if isinstance(value, Mapping):
        return any(
            _contains_oauth_identifier(k) or _contains_oauth_identifier(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_oauth_identifier(item) for item in value)
    return False


def _prune_oauth_from_value(value: Any) -> Any:
    from src.dalton.llm.overlay import _is_oauth_model_key

    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            k_str = str(k)
            if _is_oauth_model_key(k_str.strip().lower()):
                continue
            if isinstance(v, str) and _is_oauth_model_key(v.strip().lower()):
                continue
            pruned = _prune_oauth_from_value(v)
            if isinstance(v, Mapping) and not pruned:
                continue
            out[k_str] = pruned
        return out
    if isinstance(value, list):
        kept = []
        for item in value:
            if isinstance(item, str) and _is_oauth_model_key(item.strip().lower()):
                continue
            pruned = _prune_oauth_from_value(item)
            if isinstance(item, Mapping) and not pruned:
                continue
            kept.append(pruned)
        return kept
    return copy.deepcopy(value)


_PUBLIC_REVISION_RECORD_FIELDS = (
    "revision",
    "action",
    "target_keys",
    "payload_sha256",
    "diff_summary",
    "side_effects",
    "comment",
    "restored_from",
    "actor_user_id",
    "actor_label",
    "created_at",
)


def sanitize_revision_record_for_capabilities(
    rec: Mapping[str, Any],
    *,
    oauth_sync_enabled: bool,
) -> dict[str, Any]:
    """Project UI-only revision metadata and prune ``*-oauth`` identifiers when ``native_oauth_sync`` is off.

    Never returns raw historical ``payload`` (including historical model parameters) to
    the browser; rollback continues to read the full revision row server-side via
    ``get_catalog_revision``.
    """
    out: dict[str, Any] = {
        field: copy.deepcopy(rec[field])
        for field in _PUBLIC_REVISION_RECORD_FIELDS
        if field in rec
    }
    if oauth_sync_enabled:
        return out

    from src.dalton.llm.overlay import _is_oauth_model_key

    raw_targets = out.get("target_keys")
    if isinstance(raw_targets, list):
        out["target_keys"] = [
            str(k)
            for k in raw_targets
            if not _is_oauth_model_key(str(k).strip().lower())
        ]

    raw_diff = out.get("diff_summary")
    if isinstance(raw_diff, Mapping):
        out["diff_summary"] = _prune_oauth_from_value(raw_diff)

    raw_effects = out.get("side_effects")
    if isinstance(raw_effects, list):
        out["side_effects"] = [
            eff
            for eff in raw_effects
            if not _contains_oauth_identifier(eff)
        ]

    return out


def _merge_model_override_patch(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    """Deep-merge ``patch`` into a ``model_overrides`` dict while preserving explicit ``None`` markers."""
    for key, value in patch.items():
        if value is None:
            target[key] = None
        elif isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_model_override_patch(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_str(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# In-lock model validation helper for all outermost shared-lock writers (§2.7 #3)
# ---------------------------------------------------------------------------


async def assert_model_not_retired_under_lock(
    conn: Any,
    model_key: str | None,
    *,
    field: str = "llm_model",
    user_id: str | None = None,
    model_pref: Mapping[str, Any] | None = None,
    allow_custom: bool = False,
) -> CompiledCatalog | None:
    """Verify ``model_key`` is active (exists and not retired) in the live catalog under a held catalog shared lock.

    Raises:
        HTTPException(400, detail={"code": "model_retired", ...}) if retired.
        HTTPException(400, detail={"code": "model_unavailable", ...}) if absent from the
            active catalog (e.g. removed by a rollback) and not an allowed custom model.
    """
    cleaned = str(model_key or "").strip()
    if not cleaned:
        return None
    compiled = await get_compiled_catalog_async(conn=conn)
    if not compiled.from_db:
        return None
    # Personal custom models/providers shadow team catalog names (matching classify_model);
    # check them BEFORE team retirement checks so retiring a team model never blocks a user's
    # same-name custom model or custom provider.
    if allow_custom:
        from src.llms.preferences import custom_model_names
        from src.server.services.llm import user_models

        pref: Mapping[str, Any] | None = model_pref
        if pref is None and user_id:
            try:
                pref = await user_models.get_model_preference(user_id)
            except Exception:
                pref = None
        if isinstance(pref, Mapping):
            if cleaned in custom_model_names(dict(pref)):
                return compiled
            raw_cps = pref.get("custom_providers")
            if isinstance(raw_cps, list) and any(
                isinstance(cp, Mapping) and str(cp.get("name") or "") == cleaned
                for cp in raw_cps
            ):
                return compiled
        if user_id:
            try:
                from src.dalton.credentials import (
                    credential_user_id,
                    uses_shared_team_credential,
                )

                if uses_shared_team_credential(user_id):
                    holder_id = credential_user_id(user_id)
                    if holder_id and holder_id != user_id:
                        holder_cp = await user_models.get_custom_provider_config(
                            holder_id, cleaned
                        )
                        if holder_cp:
                            return compiled
            except Exception:
                pass
    all_retired = (
        compiled.factory_retired_keys
        | compiled.readonly_legacy_retired_keys
        | compiled.team_retired_keys
    )
    if cleaned in all_retired:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "model_retired",
                "field": field,
                "model": cleaned,
                "message": f"Model '{cleaned}' has been retired and cannot be assigned.",
            },
        )
    if compiled.model_config.get_model_config(cleaned) is not None:
        return compiled
    raise HTTPException(
        status_code=400,
        detail={
            "code": "model_unavailable",
            "field": field,
            "model": cleaned,
            "message": f"Model '{cleaned}' is not available in the active catalog and cannot be assigned.",
        },
    )


# ---------------------------------------------------------------------------
# Six-source reference classification (§2.7 #2)
# ---------------------------------------------------------------------------


async def collect_classified_model_references(
    target_models: set[str],
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Collect and classify references across all 6 sources for ``target_models``.

    Raises ``ReferenceSourceUnavailable`` (fail-closed 503) if ANY source cannot be read.
    """
    if not target_models:
        return {
            "agent_config": {},
            "dalton_settings": {},
            "bundled_plugin_subagents": {},
            "plugin_subagents": [],
            "user_subagents": [],
            "automations": [],
            "user_preferences": [],
        }

    # ① agent_config (hard block)
    raw_agent = model_references.agent_config_refs()
    agent_hits = {k: list(raw_agent[k]) for k in target_models if k in raw_agent}

    # ② dalton_settings (hard block)
    raw_settings = model_references.dalton_settings_refs()
    settings_hits = {k: list(raw_settings[k]) for k in target_models if k in raw_settings}

    # ③a bundled plugin .md agents (hard block)
    bundled_raw: dict[str, list[str]] = {}
    try:
        model_references._bundled_agent_model_refs(bundled_raw)
    except ReferenceSourceUnavailable:
        raise
    except Exception as exc:
        raise ReferenceSourceUnavailable("plugin_subagents", exc) from exc
    bundled_hits = {k: list(bundled_raw[k]) for k in target_models if k in bundled_raw}

    target_list = sorted(target_models)
    plugin_db_rows: list[dict[str, Any]] = []
    user_subagent_rows: list[dict[str, Any]] = []
    automation_rows: list[dict[str, Any]] = []
    user_pref_rows: list[dict[str, Any]] = []

    async with get_db_connection(conn) as db:
        # ③b DB plugin_subagents
        try:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT s.plugin_subagent_id::text AS plugin_subagent_id,
                           s.user_id, s.plugin_id::text AS plugin_id,
                           p.name AS plugin_name, s.name, s.fields, s.updated_at
                    FROM plugin_subagents s
                    JOIN user_plugins p
                      ON p.user_id = s.user_id AND p.user_plugin_id = s.plugin_id
                    WHERE s.fields->>'model' = ANY(%s)
                    ORDER BY s.user_id, s.name
                    """,
                    (target_list,),
                )
                for r in await cur.fetchall():
                    plugin_db_rows.append(
                        {
                            "plugin_subagent_id": r["plugin_subagent_id"],
                            "user_id": str(r["user_id"]),
                            "plugin_id": r["plugin_id"],
                            "plugin_name": str(r["plugin_name"]),
                            "name": str(r["name"]),
                            "model": str((r.get("fields") or {}).get("model") or ""),
                            "fields": dict(r.get("fields") or {}),
                            "updated_at": _ts_str(r.get("updated_at")),
                        }
                    )
        except Exception as exc:
            raise ReferenceSourceUnavailable("plugin_subagents", exc) from exc

        # ④ DB user_subagents (custom + plugin overrides)
        try:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_subagent_id::text AS user_subagent_id,
                           user_id, name, kind, base_key, fields, updated_at
                    FROM user_subagents
                    WHERE fields->>'model' = ANY(%s)
                    ORDER BY user_id, name
                    """,
                    (target_list,),
                )
                for r in await cur.fetchall():
                    user_subagent_rows.append(
                        {
                            "user_subagent_id": r["user_subagent_id"],
                            "user_id": str(r["user_id"]),
                            "name": str(r["name"]),
                            "kind": str(r["kind"]),
                            "base_key": r.get("base_key"),
                            "model": str((r.get("fields") or {}).get("model") or ""),
                            "fields": dict(r.get("fields") or {}),
                            "updated_at": _ts_str(r.get("updated_at")),
                        }
                    )
        except Exception as exc:
            raise ReferenceSourceUnavailable("user_subagents", exc) from exc

        # ⑤ DB automations (non-deleted rows explicitly naming llm_model)
        try:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT automation_id::text AS automation_id,
                           user_id, name, file_name, workspace_id::text AS workspace_id,
                           llm_model, reasoning_effort, config_version, updated_at
                    FROM automations
                    WHERE llm_model = ANY(%s)
                      AND deleted_at IS NULL
                    ORDER BY user_id, automation_id
                    """,
                    (target_list,),
                )
                for r in await cur.fetchall():
                    automation_rows.append(
                        {
                            "automation_id": r["automation_id"],
                            "user_id": str(r["user_id"]),
                            "name": str(r.get("name") or ""),
                            "file_name": r.get("file_name"),
                            "workspace_id": r.get("workspace_id"),
                            "llm_model": str(r["llm_model"]),
                            "reasoning_effort": r.get("reasoning_effort"),
                            "config_version": int(r.get("config_version") or 1),
                            "updated_at": _ts_str(r.get("updated_at")),
                        }
                    )
        except Exception as exc:
            raise ReferenceSourceUnavailable("automations", exc) from exc

        # ⑥ DB user_preferences (default slots & subagent_models)
        try:
            async with db.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_id, model_preference, agent_preference, updated_at
                    FROM user_preferences
                    """
                )
                for r in await cur.fetchall():
                    mp = r.get("model_preference") if isinstance(r.get("model_preference"), dict) else {}
                    ap = r.get("agent_preference") if isinstance(r.get("agent_preference"), dict) else {}
                    matched_slots: list[str] = []
                    for sk in _USER_PREF_SLOT_KEYS:
                        if mp.get(sk) in target_models:
                            matched_slots.append(f"model_preference.{sk}")
                    sub_models = ap.get("subagent_models")
                    if isinstance(sub_models, dict):
                        for sname, smodel in sub_models.items():
                            if smodel in target_models:
                                matched_slots.append(f"agent_preference.subagent_models.{sname}")
                    if matched_slots:
                        user_pref_rows.append(
                            {
                                "user_id": str(r["user_id"]),
                                "slots": matched_slots,
                                "model_preference": copy.deepcopy(mp),
                                "agent_preference": copy.deepcopy(ap),
                                "updated_at": _ts_str(r.get("updated_at")),
                            }
                        )
        except Exception as exc:
            raise ReferenceSourceUnavailable("user_preferences", exc) from exc

    return {
        "agent_config": agent_hits,
        "dalton_settings": settings_hits,
        "bundled_plugin_subagents": bundled_hits,
        "plugin_subagents": plugin_db_rows,
        "user_subagents": user_subagent_rows,
        "automations": automation_rows,
        "user_preferences": user_pref_rows,
    }


# ---------------------------------------------------------------------------
# Version-aligned curated setup helper (§5.4 #2)
# ---------------------------------------------------------------------------


def derive_curated_setup_for_catalog(compiled: CompiledCatalog) -> dict[str, Any]:
    """Compute ``dalton_setup_candidates`` & ``dalton_curated_providers`` from ``compiled``."""
    from src.dalton.profile import DeploymentProfile, resolve_profile

    mc = compiled.model_config
    llm_cfg = mc.llm_config or {}
    meta = compiled.dalton_meta or {}
    is_core = resolve_profile() == DeploymentProfile.CORE

    raw_candidates = (
        list(compiled.curated_setup)
        if compiled.curated_setup is not None
        else list(get_default_curated_setup_candidates())
    )
    candidates: list[str] = []
    providers: list[str] = []
    for key in raw_candidates:
        entry = llm_cfg.get(key)
        if not isinstance(entry, Mapping):
            continue
        if entry.get("visible") is False:
            continue
        if is_core:
            m_meta = meta.get(key) if isinstance(meta.get(key), Mapping) else {}
            if m_meta.get("tool_calling") is False:
                continue
        candidates.append(key)
        prov = str(entry.get("provider") or "").strip()
        if prov and prov not in providers:
            providers.append(prov)
    return {
        "dalton_setup_candidates": candidates,
        "dalton_curated_providers": providers,
    }


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


async def get_team_catalog_overview() -> dict[str, Any]:
    """Return full admin view of the team model catalog at the current PostgreSQL revision."""
    compiled = await get_compiled_catalog_async()
    mc = compiled.model_config
    creds = await list_catalog_provider_credentials()
    row = await get_catalog_row()

    # Connected providers check (best-effort non-blocking)
    connected_providers: set[str] = set()
    try:
        from src.dalton.credentials import get_team_credential_holder_id_async
        from src.server.database.api_keys import get_user_api_keys

        holder_id = await get_team_credential_holder_id_async()
        if holder_id:
            byok = await get_user_api_keys(holder_id)
            connected_providers.update(
                p for p, k in (byok.get("keys") or {}).items() if k
            )
    except Exception:
        pass
    for slug in creds:
        connected_providers.add(slug)

    db_models = (compiled.payload.get("models") or {}) if isinstance(compiled.payload, Mapping) else {}
    db_overrides = (
        (compiled.payload.get("model_overrides") or {})
        if isinstance(compiled.payload, Mapping)
        else {}
    )
    compiled_bounds = (
        compiled.task_costs.bounds
        if compiled.task_costs is not None and isinstance(compiled.task_costs.bounds, Mapping)
        else {}
    )
    raw_db_tc = compiled.payload.get("task_costs") if isinstance(compiled.payload, Mapping) else None
    raw_db_bounds = (
        raw_db_tc.get("bounds")
        if isinstance(raw_db_tc, Mapping) and isinstance(raw_db_tc.get("bounds"), Mapping)
        else {}
    )

    items: list[dict[str, Any]] = []
    for key, entry in (mc.llm_config or {}).items():
        if not isinstance(entry, Mapping):
            continue
        prov = str(entry.get("provider") or "")
        prov_info = mc.get_provider_info(prov)
        model_id = str(entry.get("model_id") or key)
        meta_entry = compiled.dalton_meta.get(key) or {}
        cash_card = mc.get_model_info(prov, model_id) or _find_provider_pricing_card(
            mc, prov, model_id
        )
        raw_aliases = (cash_card or {}).get("alias") if isinstance(cash_card, Mapping) else None
        aliases = (
            [str(a).strip() for a in raw_aliases if isinstance(a, str) and str(a).strip()]
            if isinstance(raw_aliases, list)
            else []
        )
        bound_out: dict[str, Any] | None = None
        if key in compiled_bounds:
            b_model, b_times = compiled_bounds[key]
            bound_out = {"model": str(b_model), "times": float(b_times)}
        elif key in raw_db_bounds and isinstance(raw_db_bounds[key], Mapping):
            raw_b = raw_db_bounds[key]
            if (
                isinstance(raw_b.get("model"), str)
                and raw_b["model"].strip()
                and isinstance(raw_b.get("times"), (int, float))
                and not isinstance(raw_b.get("times"), bool)
            ):
                bound_out = {"model": raw_b["model"].strip(), "times": float(raw_b["times"])}
        is_variant = bool(prov_info.get("catalog_defined"))
        source = "team_added" if key in db_models else ("team_overridden" if key in db_overrides else "factory")
        prompt_guidance = entry.get("prompt_guidance")
        items.append(
            {
                "model_key": key,
                "model_id": model_id,
                "provider": prov,
                "provider_sdk": prov_info.get("sdk"),
                "provider_display_name": prov_info.get("display_name") or prov,
                "provider_connected": prov in connected_providers,
                "catalog_defined_provider": is_variant,
                "display_name": entry.get("display_name") or key,
                "visible": entry.get("visible", True) is not False,
                "source": source,
                "status": "visible" if entry.get("visible", True) is not False else "hidden",
                "intelligence": entry.get("intelligence"),
                "speed": entry.get("speed"),
                "plain_language": entry.get("plain_language"),
                "context": entry.get("context"),
                "compaction_profile": entry.get("compaction_profile"),
                "input_modalities": entry.get("input_modalities") or ["text"],
                "reasoning": entry.get("reasoning"),
                "parameters": _sanitize_public_parameters(entry.get("parameters")),
                "prompt_guidance": prompt_guidance if isinstance(prompt_guidance, str) else None,
                "task_costs_bound": bound_out,
                "alias": aliases or None,
                "pricing": (cash_card or {}).get("pricing") if isinstance(cash_card, Mapping) else None,
                "pricing_meta": (cash_card or {}).get("pricing_meta") if isinstance(cash_card, Mapping) else None,
                "dalton_meta": meta_entry,
            }
        )

    retired_items: list[dict[str, Any]] = []
    for rec in (compiled.payload.get("retired") or []):
        if isinstance(rec, Mapping) and rec.get("key"):
            rkey = str(rec["key"])
            retired_items.append(
                {
                    "key": rkey,
                    "provider": str(rec.get("provider") or ""),
                    "retired_at": rec.get("retired_at"),
                    "retired_by": rec.get("retired_by"),
                    "scope": "readonly_legacy"
                    if rkey in compiled.readonly_legacy_retired_keys
                    else "team",
                    "restorable": rkey not in compiled.readonly_legacy_retired_keys
                    and rkey not in compiled.factory_retired_keys,
                }
            )
    for rkey in sorted(compiled.readonly_legacy_retired_keys):
        if not any(x["key"] == rkey for x in retired_items):
            retired_items.append(
                {
                    "key": rkey,
                    "provider": "",
                    "retired_at": None,
                    "retired_by": None,
                    "scope": "readonly_legacy",
                    "restorable": False,
                }
            )

    variants_out: list[dict[str, Any]] = []
    for slug, vinfo in compiled.provider_variants.items():
        cred_meta = creds.get(slug)
        variants_out.append(
            {
                "provider_slug": slug,
                "display_name": vinfo.get("display_name") or slug,
                "sdk": vinfo.get("sdk"),
                "parent_provider": vinfo.get("parent_provider"),
                "access_type": vinfo.get("access_type", "api_key"),
                "billing": vinfo.get("billing", "cash"),
                "base_url": vinfo.get("base_url"),
                "credential_configured": cred_meta is not None,
                "key_fingerprint": (cred_meta or {}).get("key_fingerprint"),
                "bound_base_url": (cred_meta or {}).get("bound_base_url"),
                "updated_at": (cred_meta or {}).get("updated_at"),
            }
        )

    from src.dalton.llm.overlay import _is_oauth_model_key

    prov_cfg = (mc.manifest or {}).get("provider_config") or {}
    parent_provider_groups = sorted(
        str(g).strip()
        for g in prov_cfg
        if isinstance(g, str)
        and str(g).strip()
        and (compiled.oauth_sync_enabled or not _is_oauth_model_key(str(g).strip().lower()))
    )
    supported_variant_sdks = sorted(_SUPPORTED_VARIANT_SDKS)

    return {
        "revision": compiled.revision,
        "payload_sha256": compiled.payload_sha256,
        "legacy_sync_import_status": compiled.legacy_sync_import_status,
        "legacy_sync_import_error": (row or {}).get("legacy_sync_import_error"),
        "models": items,
        "retired": retired_items,
        "provider_variants": variants_out,
        "parent_provider_groups": parent_provider_groups,
        "supported_variant_sdks": supported_variant_sdks,
        "curated_setup": compiled.curated_setup,
        **derive_curated_setup_for_catalog(compiled),
    }


async def list_revisions_view(*, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    compiled = await get_compiled_catalog_async()
    revs = await list_catalog_revisions(limit=limit, offset=offset)
    sanitized = [
        sanitize_revision_record_for_capabilities(
            r, oauth_sync_enabled=compiled.oauth_sync_enabled
        )
        for r in revs
    ]
    return {"revision": compiled.revision, "revisions": sanitized}


# ---------------------------------------------------------------------------
# Provider variant upsert (§2.5, §3.1, §3.2)
# ---------------------------------------------------------------------------


async def upsert_provider_variant(
    *,
    actor_user_id: str,
    data: ProviderVariantUpsertInput,
    is_update: bool = False,
) -> dict[str, Any]:
    """Create or update a catalog-defined provider variant with write-only encrypted credential."""
    # Pre-lock preflight URL connectivity/public-IP check (Fake-IP DoH or public unicast)
    try:
        await preflight_catalog_variant_url(data.base_url)
    except (ValueError, EgressBlockedError) as exc:
        raise CatalogValidationError("variant_base_url_blocked", str(exc)) from exc

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )

            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            cleaned_url, canonical_bound = validate_provider_variant_input(
                data, compiled=current_compiled, is_update=is_update
            )
            slug = data.provider_slug.strip()
            existing_creds = await list_catalog_provider_credentials(conn=conn)
            existing_cred = existing_creds.get(slug)

            # §2.5 #3: modifying base_url requires submitting a fresh credential bound to the new URL
            url_changed = (
                existing_cred is not None
                and existing_cred.get("canonical_bound_url") != canonical_bound
            )
            if (existing_cred is None or url_changed) and not (data.api_key and data.api_key.strip()):
                raise CatalogValidationError(
                    "variant_api_key_required",
                    f"Setting or changing base_url for catalog variant '{slug}' requires a dedicated api_key.",
                )

            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA
            prov_block = next_payload.setdefault("providers", {})
            variants_by_group = prov_block.setdefault("provider_variants", {})
            # Remove from any prior group if moved
            for grp, vmap in list(variants_by_group.items()):
                if isinstance(vmap, dict) and slug in vmap and grp != data.parent_provider:
                    vmap.pop(slug, None)
            grp_map = variants_by_group.setdefault(data.parent_provider, {})
            grp_map[slug] = {
                "display_name": data.display_name.strip(),
                "sdk": data.sdk.strip(),
                "parent_provider": data.parent_provider.strip(),
                "access_type": data.access_type,
                "billing": data.billing,
                "base_url": cleaned_url,
            }

            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )

            cred_updated = False
            key_fp: str | None = (existing_cred or {}).get("key_fingerprint")
            if data.api_key and data.api_key.strip():
                cred_row = await upsert_catalog_provider_credential(
                    conn,
                    provider_slug=slug,
                    api_key=data.api_key.strip(),
                    bound_base_url=cleaned_url,
                    updated_by=actor_user_id,
                )
                cred_updated = True
                key_fp = cred_row["key_fingerprint"]

            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="upsert_provider_variant",
                target_keys=[slug],
                payload=next_payload,
                diff_summary={
                    "provider_slug": slug,
                    "parent_provider": data.parent_provider,
                    "base_url": cleaned_url,
                    "credential_updated": cred_updated,
                    "key_fingerprint": key_fp,
                },
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        "provider_slug": slug,
        "credential_configured": True,
        "key_fingerprint": key_fp,
        "revision_record": saved["revision_record"],
    }


# ---------------------------------------------------------------------------
# Model create / update / hide / unhide / restore-factory-default
# ---------------------------------------------------------------------------


def _build_pricing_card_dict(
    model_id: str,
    display_name: str,
    data: CatalogModelUpsertInput,
) -> dict[str, Any] | None:
    if data.pricing is None:
        return None
    raw_pricing = data.pricing.model_dump(exclude_none=True)
    meta_fields: dict[str, Any] = {}
    for mf in ("source_kind", "estimated", "source", "source_url", "verified_at", "note"):
        if mf in raw_pricing:
            meta_fields[mf] = raw_pricing.pop(mf)
    if data.pricing_meta is not None:
        meta_fields.update(data.pricing_meta.model_dump(exclude_none=True))
    if meta_fields.get("source_kind") == "estimated" and "estimated" not in meta_fields:
        meta_fields["estimated"] = True
    normalized_pricing = normalize_cash_pricing_card(raw_pricing) or {}
    card: dict[str, Any] = {
        "id": model_id,
        "name": display_name,
        "pricing": normalized_pricing,
    }
    if data.alias:
        card["alias"] = list(data.alias)
    if meta_fields:
        card["pricing_meta"] = meta_fields
    if data.context is not None:
        card["context_window"] = data.context
    params = data.parameters or {}
    if params.get("max_tokens") or params.get("max_output_tokens"):
        card["max_output"] = params.get("max_tokens") or params.get("max_output_tokens")
    return card


async def _apply_inline_provider_variant_update_in_tx(
    conn: Any,
    *,
    variant_in: InlineProviderVariantUpdateInput,
    target_provider: str,
    current_compiled: CompiledCatalog,
    next_payload: dict[str, Any],
    actor_user_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and apply an inline catalog provider variant update inside an active catalog transaction.

    Because the credential upsert runs on ``conn`` inside the caller's ``conn.transaction()``,
    any subsequent model validation or trial-compile failure rolls back both the variant
    payload and the credential write atomically.
    """
    slug = variant_in.provider_slug.strip()
    if slug != target_provider.strip():
        raise CatalogValidationError(
            "variant_provider_mismatch",
            f"provider_variant_update.provider_slug ({slug!r}) must match model provider ({target_provider!r}).",
        )

    cleaned_url, canonical_bound = validate_provider_variant_input(
        variant_in, compiled=current_compiled, is_update=True
    )
    existing_creds = await list_catalog_provider_credentials(conn=conn)
    existing_cred = existing_creds.get(slug)

    url_changed = (
        existing_cred is not None
        and existing_cred.get("canonical_bound_url") != canonical_bound
    )
    if (existing_cred is None or url_changed) and not (
        variant_in.api_key and variant_in.api_key.strip()
    ):
        raise CatalogValidationError(
            "variant_api_key_required",
            f"Setting or changing base_url for catalog variant '{slug}' requires a dedicated api_key.",
        )

    prov_block = next_payload.setdefault("providers", {})
    variants_by_group = prov_block.setdefault("provider_variants", {})
    for grp, vmap in list(variants_by_group.items()):
        if isinstance(vmap, dict) and slug in vmap and grp != variant_in.parent_provider:
            vmap.pop(slug, None)
    grp_map = variants_by_group.setdefault(variant_in.parent_provider, {})
    variant_entry = {
        "display_name": variant_in.display_name.strip(),
        "sdk": variant_in.sdk.strip(),
        "parent_provider": variant_in.parent_provider.strip(),
        "access_type": variant_in.access_type,
        "billing": variant_in.billing,
        "base_url": cleaned_url,
    }
    grp_map[slug] = variant_entry

    cred_updated = False
    key_fp: str | None = (existing_cred or {}).get("key_fingerprint")
    if variant_in.api_key and variant_in.api_key.strip():
        cred_row = await upsert_catalog_provider_credential(
            conn,
            provider_slug=slug,
            api_key=variant_in.api_key.strip(),
            bound_base_url=cleaned_url,
            updated_by=actor_user_id,
        )
        cred_updated = True
        key_fp = cred_row["key_fingerprint"]

    updated_prov_info = {
        **variant_entry,
        "catalog_defined": True,
        "own_api_key": True,
        "group": variant_in.parent_provider.strip(),
    }
    variant_diff = {
        "provider_slug": slug,
        "parent_provider": variant_in.parent_provider.strip(),
        "base_url": cleaned_url,
        "credential_updated": cred_updated,
        "key_fingerprint": key_fp,
    }
    return updated_prov_info, variant_diff


def _apply_curated_setup_member_in_tx(
    next_payload: dict[str, Any],
    *,
    current_compiled: CompiledCatalog,
    model_key: str,
    include: bool,
) -> list[str]:
    """Add or remove ``model_key`` in ``next_payload['curated_setup']`` within the same transaction."""
    if isinstance(next_payload.get("curated_setup"), list):
        base_list = [str(k).strip() for k in next_payload["curated_setup"] if str(k).strip()]
    else:
        base_list = list(derive_curated_setup_for_catalog(current_compiled)["dalton_setup_candidates"])

    if include:
        next_list = list(base_list)
        if model_key not in next_list:
            next_list.append(model_key)
    else:
        next_list = [k for k in base_list if k != model_key]
    next_payload["curated_setup"] = next_list
    return next_list


async def create_catalog_model(
    *,
    actor_user_id: str,
    data: CatalogModelUpsertInput,
) -> dict[str, Any]:
    """Create a new model in the team catalog (returns 409 if key exists in Layers 1–3).

    When ``provider_variant_update`` and/or ``curated_setup_member`` are supplied,
    all changes execute atomically inside the same PostgreSQL transaction and
    catalog revision bump.
    """
    model_key = str(data.model_key or "").strip()
    if not model_key:
        raise CatalogValidationError("missing_model_key", "model_key is required")
    if not data.model_id or not data.provider:
        raise CatalogValidationError("missing_fields", "model_id and provider are required")

    if data.provider_variant_update is not None:
        if data.provider_variant_update.provider_slug.strip() != data.provider.strip():
            raise CatalogValidationError(
                "variant_provider_mismatch",
                f"provider_variant_update.provider_slug ({data.provider_variant_update.provider_slug.strip()!r}) "
                f"must match model provider ({data.provider.strip()!r}).",
            )
        try:
            await preflight_catalog_variant_url(data.provider_variant_update.base_url)
        except (ValueError, EgressBlockedError) as exc:
            raise CatalogValidationError("variant_base_url_blocked", str(exc)) from exc

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )
            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            mc = current_compiled.model_config
            # Dimension ①: reject collision with existing Layer 1–3 keys
            existing_lower = {k.lower(): k for k in mc.llm_config}
            if model_key.lower() in existing_lower:
                raise CatalogConflictError(
                    "model_key_exists",
                    f"Model key {model_key!r} already exists as {existing_lower[model_key.lower()]!r}; use update instead.",
                )

            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA

            variant_diff: dict[str, Any] | None = None
            if data.provider_variant_update is not None:
                prov_info, variant_diff = await _apply_inline_provider_variant_update_in_tx(
                    conn,
                    variant_in=data.provider_variant_update,
                    target_provider=data.provider.strip(),
                    current_compiled=current_compiled,
                    next_payload=next_payload,
                    actor_user_id=actor_user_id,
                )
            else:
                prov_info = mc.get_provider_info(data.provider)
                if not prov_info:
                    raise CatalogValidationError(
                        "unknown_provider", f"Unknown provider {data.provider!r}"
                    )

            model_entry: dict[str, Any] = {
                "model_id": data.model_id.strip(),
                "provider": data.provider.strip(),
                "display_name": (data.display_name or model_key).strip(),
                "visible": True if data.visible is None else bool(data.visible),
            }
            for fname in (
                "prompt_guidance",
                "intelligence",
                "speed",
                "plain_language",
                "context",
                "compaction_profile",
                "input_modalities",
                "parameters",
                "extra_body",
                "reasoning",
            ):
                val = getattr(data, fname)
                if val is not None:
                    copied = copy.deepcopy(val)
                    if fname in ("parameters", "reasoning") and isinstance(copied, dict):
                        for sub_k in [k for k, v in copied.items() if v is None]:
                            copied.pop(sub_k, None)
                        if not copied:
                            continue
                    model_entry[fname] = copied

            dm_entry = (
                data.dalton_meta.model_dump(exclude_unset=True)
                if data.dalton_meta is not None
                else None
            )
            if isinstance(dm_entry, dict):
                dm_entry = {k: v for k, v in dm_entry.items() if v is not None}
                if not dm_entry:
                    dm_entry = None
            pricing_dict = (
                data.pricing.model_dump(exclude_none=True) if data.pricing is not None else None
            )
            validate_model_fields(
                model_key=model_key,
                model_entry=model_entry,
                pricing_card=pricing_dict,
                dalton_meta_entry=dm_entry,
                provider_info=prov_info,
                pricing_explicit=(data.pricing is not None),
            )

            next_payload.setdefault("models", {})[model_key] = model_entry

            card_obj = _build_pricing_card_dict(
                model_entry["model_id"], model_entry["display_name"], data
            )
            if card_obj is not None:
                prov_models = next_payload.setdefault("providers", {}).setdefault("models", {})
                cards = list(prov_models.get(data.provider) or [])
                cards.append(card_obj)
                prov_models[data.provider] = cards

            if dm_entry is not None:
                next_payload.setdefault("dalton_meta", {})[model_key] = dm_entry

            if data.task_costs_bound is not None:
                next_payload.setdefault("task_costs", {}).setdefault("bounds", {})[model_key] = (
                    copy.deepcopy(data.task_costs_bound)
                )

            curated_updated: list[str] | None = None
            if data.curated_setup_member is not None:
                curated_updated = _apply_curated_setup_member_in_tx(
                    next_payload,
                    current_compiled=current_compiled,
                    model_key=model_key,
                    include=bool(data.curated_setup_member),
                )

            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            diff_summary: dict[str, Any] = {"created": model_key, "provider": data.provider}
            if variant_diff is not None:
                diff_summary["provider_variant"] = variant_diff
            if curated_updated is not None:
                diff_summary["curated_setup"] = curated_updated
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="create_model",
                target_keys=[model_key],
                payload=next_payload,
                diff_summary=diff_summary,
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        "model_key": model_key,
        "revision_record": saved["revision_record"],
    }


def _find_provider_pricing_card(
    mc: Any,
    provider: str,
    model_id: str,
) -> dict[str, Any] | None:
    """Locate a pricing card under ``provider`` matching ``norm_id(id)`` or any ``norm_id(alias)``."""
    manifest_models = (getattr(mc, "manifest", None) or {}).get("models") or {}
    cards = manifest_models.get(provider) if isinstance(manifest_models, Mapping) else None
    if not isinstance(cards, list):
        return None
    target = norm_id(model_id)
    if not target:
        return None
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        if norm_id(str(card.get("id") or "")) == target:
            return dict(card)
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        aliases = card.get("alias") or []
        if isinstance(aliases, list) and any(norm_id(str(a)) == target for a in aliases if a):
            return dict(card)
    return None


_PUBLIC_PRICE_SOURCE_KINDS = frozenset({"official", "estimated", "third_party"})


def _extract_public_price_source(raw: Any) -> dict[str, Any]:
    """Return read-only price source fields (``source_kind``, ``estimated``) only."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    sk = raw.get("source_kind")
    if isinstance(sk, str) and sk in _PUBLIC_PRICE_SOURCE_KINDS:
        out["source_kind"] = sk
    est = raw.get("estimated")
    if isinstance(est, bool):
        out["estimated"] = est
    return out


def _find_manifest_card_by_pricing_identity(
    model_config: Any,
    pricing_dict: Any,
) -> Mapping[str, Any] | None:
    """Locate the manifest card whose ``pricing`` field is the exact object returned by ``find_model_pricing``."""
    if not isinstance(pricing_dict, Mapping) or not pricing_dict:
        return None
    manifest = getattr(model_config, "manifest", None)
    manifest_models = manifest.get("models") if isinstance(manifest, Mapping) else None
    if not isinstance(manifest_models, Mapping):
        return None
    for cards in manifest_models.values():
        if not isinstance(cards, list):
            continue
        for card in cards:
            if isinstance(card, Mapping) and card.get("pricing") is pricing_dict:
                return card
    return None


_PRICE_SOURCE_CACHE_MAXSIZE = 8
_PRICE_SOURCE_CACHE_LOCK = threading.Lock()
_PRICE_SOURCE_CACHE: OrderedDict[
    tuple[Any, ...], dict[str, tuple[str, dict[str, Any]]]
] = OrderedDict()


def clear_price_source_cache() -> None:
    """Clear the process-local per-revision price-source annotation cache."""
    with _PRICE_SOURCE_CACHE_LOCK:
        _PRICE_SOURCE_CACHE.clear()


def _compute_price_source_for_entries(
    entries: Mapping[str, Mapping[str, Any]],
    *,
    model_config: Any,
    dalton_meta_map: dict[str, dict[str, Any]],
    oauth_sync_enabled: bool,
    require_entry_pricing_for_cash: bool,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Compute ``{model_key: ("pricing_meta" | "notional_pricing", source_dict)}`` with zero DB queries."""
    from src.dalton.llm.notional_pricing import (
        build_notional_price_index_for_catalog,
        resolve_notional_card,
    )
    from src.dalton.services.team_model_catalog.billing import (
        bind_billing_dalton_meta,
        resolve_model_billing,
    )
    from src.llms.pricing_utils import find_model_pricing

    llm_cfg = getattr(model_config, "llm_config", None)
    llm_cfg_map = llm_cfg if isinstance(llm_cfg, Mapping) else {}
    notional_idx: dict[str, dict[str, Any]] | None = None
    result: dict[str, tuple[str, dict[str, Any]]] = {}

    with bind_billing_dalton_meta(dalton_meta_map):
        for model_key, entry in entries.items():
            if not isinstance(entry, Mapping):
                continue

            cfg_entry = llm_cfg_map.get(model_key)
            cfg_map = cfg_entry if isinstance(cfg_entry, Mapping) else {}
            provider = str(entry.get("provider") or cfg_map.get("provider") or "")
            model_id = str(entry.get("model_id") or cfg_map.get("model_id") or model_key)

            is_sub = (
                resolve_model_billing(
                    model_key=model_key,
                    provider=provider or None,
                    model_id=model_id,
                    model_config=model_config,
                    dalton_meta=dalton_meta_map,
                )
                == "subscription"
            )

            if is_sub:
                if notional_idx is None:
                    notional_idx = build_notional_price_index_for_catalog(
                        model_config=model_config,
                        dalton_meta=dalton_meta_map,
                        oauth_sync_enabled=oauth_sync_enabled,
                        include_source_meta=True,
                    )
                notional_card = resolve_notional_card(
                    model_key,
                    pricing_model_id=model_id,
                    pricing_provider=provider or None,
                    index=notional_idx,
                    model_config=model_config,
                )
                np_out = _extract_public_price_source(notional_card)
                if np_out:
                    result[model_key] = ("notional_pricing", np_out)
                continue

            if require_entry_pricing_for_cash and not (
                isinstance(entry.get("pricing"), Mapping) and bool(entry.get("pricing"))
            ):
                continue

            # Match get_list_rates(...) parameters exactly (no model_key override).
            pricing_dict = find_model_pricing(
                model_id,
                provider or None,
                _warn_on_miss=False,
                model_config=model_config,
            )
            cash_card = _find_manifest_card_by_pricing_identity(model_config, pricing_dict)
            pm_out = _extract_public_price_source(
                cash_card.get("pricing_meta") if isinstance(cash_card, Mapping) else None
            )
            if pm_out:
                result[model_key] = ("pricing_meta", pm_out)

    return result


def annotate_model_metadata_price_source(
    metadata: dict[str, Any],
    *,
    model_config: Any,
    dalton_meta: Mapping[str, Any] | None = None,
    compiled_catalog: CompiledCatalog | None = None,
    oauth_sync_enabled: bool | None = None,
) -> dict[str, Any]:
    """Attach read-only ``pricing_meta`` / ``notional_pricing`` source fields to ``/api/v1/models``.

    Exposes strictly ``source_kind`` and ``estimated`` (never numeric prices or internal notes):
    - Billing classification delegates to ``resolve_model_billing`` using the request's
      ``dalton_meta`` without any DB fallback.
    - Cash models attach ``pricing_meta`` ONLY when ``metadata[key]`` already displays a cash
      ``pricing`` dict, calling unmodified ``find_model_pricing`` with the same parameters as
      ``get_list_rates`` and matching the exact manifest card by object identity
      (``card.get("pricing") is pricing_dict``).
    - Subscription models resolve via ``resolve_notional_card(..., model_config=model_config)``
      on an index built with the same ``oauth_sync_enabled`` flag as ``CompiledCatalog``.
    - When ``compiled_catalog`` is provided and matches ``model_config``, computed source badges
      are cached in-process keyed by catalog revision/identity and recomputed on revision change.
    """
    if not isinstance(metadata, dict):
        return metadata

    eff_oauth_sync = (
        bool(oauth_sync_enabled)
        if oauth_sync_enabled is not None
        else (
            bool(compiled_catalog.oauth_sync_enabled)
            if compiled_catalog is not None
            else has_capability(CapabilityId.NATIVE_OAUTH_SYNC)
        )
    )

    raw_dalton_meta = (
        dalton_meta
        if isinstance(dalton_meta, Mapping)
        else (compiled_catalog.dalton_meta if compiled_catalog is not None else None)
    )
    dalton_meta_map: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in raw_dalton_meta.items() if isinstance(v, Mapping)}
        if isinstance(raw_dalton_meta, Mapping)
        else {}
    )

    source_map: dict[str, tuple[str, dict[str, Any]]] | None = None
    if compiled_catalog is not None and model_config is compiled_catalog.model_config:
        cache_key = (
            compiled_catalog.revision,
            compiled_catalog.payload_sha256,
            compiled_catalog.overlay_stamp,
            eff_oauth_sync,
            compiled_catalog.legacy_retired_fingerprint,
            compiled_catalog.from_db,
            id(compiled_catalog.model_config),
        )
        with _PRICE_SOURCE_CACHE_LOCK:
            cached = _PRICE_SOURCE_CACHE.get(cache_key)
            if cached is not None:
                _PRICE_SOURCE_CACHE.move_to_end(cache_key)
                source_map = cached
        if source_map is None:
            llm_cfg = getattr(model_config, "llm_config", None)
            catalog_entries = llm_cfg if isinstance(llm_cfg, Mapping) else metadata
            computed = _compute_price_source_for_entries(
                catalog_entries,
                model_config=model_config,
                dalton_meta_map=dalton_meta_map,
                oauth_sync_enabled=eff_oauth_sync,
                require_entry_pricing_for_cash=False,
            )
            with _PRICE_SOURCE_CACHE_LOCK:
                _PRICE_SOURCE_CACHE[cache_key] = computed
                _PRICE_SOURCE_CACHE.move_to_end(cache_key)
                while len(_PRICE_SOURCE_CACHE) > _PRICE_SOURCE_CACHE_MAXSIZE:
                    _PRICE_SOURCE_CACHE.popitem(last=False)
            source_map = computed
    else:
        source_map = _compute_price_source_for_entries(
            metadata,
            model_config=model_config,
            dalton_meta_map=dalton_meta_map,
            oauth_sync_enabled=eff_oauth_sync,
            require_entry_pricing_for_cash=True,
        )

    out: dict[str, Any] = {}
    changed = False
    for model_key, entry in metadata.items():
        if not isinstance(entry, dict):
            out[model_key] = entry
            continue
        hit = source_map.get(model_key)
        if hit is None:
            out[model_key] = entry
            continue
        field_name, source_payload = hit
        if field_name == "pricing_meta" and not (
            isinstance(entry.get("pricing"), Mapping) and bool(entry.get("pricing"))
        ):
            out[model_key] = entry
            continue
        updated = dict(entry)
        updated[field_name] = dict(source_payload)
        out[model_key] = updated
        changed = True

    return out if changed else metadata


async def update_catalog_model(
    *,
    actor_user_id: str,
    model_key: str,
    data: CatalogModelUpsertInput,
) -> dict[str, Any]:
    """Update an existing model (writes to ``models`` for team-added or ``model_overrides`` for factory).

    Supports atomic companion ``provider_variant_update`` and ``curated_setup_member``
    inside the same PostgreSQL transaction, preserves non-mentioned nested ``parameters``
    server-side, and treats explicit ``None`` values as field deletions.
    """
    from src.dalton.services.team_model_catalog.compiled import (
        deep_merge_patch_with_null_delete,
    )

    key = model_key.strip()
    if data.provider_variant_update is not None:
        try:
            await preflight_catalog_variant_url(data.provider_variant_update.base_url)
        except (ValueError, EgressBlockedError) as exc:
            raise CatalogValidationError("variant_base_url_blocked", str(exc)) from exc

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )
            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            mc = current_compiled.model_config
            existing = mc.get_model_config(key)
            if not isinstance(existing, Mapping):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "model_not_found", "message": f"Model {key!r} not found."},
                )

            patch: dict[str, Any] = {}
            for fname in (
                "model_id",
                "provider",
                "display_name",
                "visible",
                "prompt_guidance",
                "intelligence",
                "speed",
                "plain_language",
                "context",
                "compaction_profile",
                "input_modalities",
                "parameters",
                "extra_body",
                "reasoning",
            ):
                if fname in data.model_fields_set:
                    patch[fname] = copy.deepcopy(getattr(data, fname))

            merged_entry = copy.deepcopy(dict(existing))
            deep_merge_patch_with_null_delete(merged_entry, patch)
            if merged_entry.get("parameters") == {}:
                merged_entry.pop("parameters", None)
            prov = str(merged_entry.get("provider") or "")

            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA

            variant_diff: dict[str, Any] | None = None
            if data.provider_variant_update is not None:
                prov_info, variant_diff = await _apply_inline_provider_variant_update_in_tx(
                    conn,
                    variant_in=data.provider_variant_update,
                    target_provider=prov,
                    current_compiled=current_compiled,
                    next_payload=next_payload,
                    actor_user_id=actor_user_id,
                )
            else:
                prov_info = mc.get_provider_info(prov)
                if not prov_info:
                    raise CatalogValidationError("unknown_provider", f"Unknown provider {prov!r}")

            dm_patch = (
                data.dalton_meta.model_dump(exclude_unset=True)
                if data.dalton_meta is not None
                else None
            )
            merged_meta = copy.deepcopy(current_compiled.dalton_meta.get(key) or {})
            if dm_patch is not None:
                deep_merge_patch_with_null_delete(merged_meta, dm_patch)

            existing_card = _find_provider_pricing_card(
                mc, prov, str(merged_entry.get("model_id") or key)
            )
            clearing_pricing = "pricing" in data.model_fields_set and data.pricing is None
            pricing_for_check = (
                None
                if clearing_pricing
                else (
                    data.pricing.model_dump(exclude_none=True)
                    if data.pricing is not None
                    else (
                        (existing_card or {}).get("pricing")
                        if isinstance(existing_card, Mapping)
                        else None
                    )
                )
            )
            validate_model_fields(
                model_key=key,
                model_entry=merged_entry,
                pricing_card=pricing_for_check,
                dalton_meta_entry=merged_meta,
                provider_info=prov_info,
                pricing_explicit=(data.pricing is not None),
            )

            if key in (next_payload.get("models") or {}):
                deep_merge_patch_with_null_delete(next_payload["models"][key], patch)
                if next_payload["models"][key].get("parameters") == {}:
                    next_payload["models"][key].pop("parameters", None)
            else:
                ov = next_payload.setdefault("model_overrides", {}).setdefault(key, {})
                _merge_model_override_patch(ov, patch)

            if clearing_pricing:
                card_id = str(merged_entry.get("model_id") or key)
                card_norm = norm_id(card_id)
                card_tokens = {card_norm}
                if isinstance(existing_card, Mapping):
                    if existing_card.get("id"):
                        card_tokens.add(norm_id(str(existing_card["id"])))
                    for al in existing_card.get("alias") or []:
                        if al:
                            card_tokens.add(norm_id(str(al)))
                prov_block = next_payload.setdefault("providers", {})
                # Only delete pricing card / overrides when no other active model on prov shares it.
                other_uses_card = any(
                    k != key
                    and isinstance(e, Mapping)
                    and str(e.get("provider") or "") == prov
                    and norm_id(str(e.get("model_id") or k)) in card_tokens
                    for k, e in (mc.llm_config or {}).items()
                )
                if not other_uses_card:
                    team_cards = (prov_block.get("models") or {}).get(prov)
                    removed_team_card = False
                    if isinstance(team_cards, list):
                        new_cards = [
                            tc
                            for tc in team_cards
                            if not (
                                isinstance(tc, Mapping)
                                and norm_id(str(tc.get("id"))) in card_tokens
                            )
                        ]
                        if len(new_cards) != len(team_cards):
                            removed_team_card = True
                            if new_cards:
                                prov_block["models"][prov] = new_cards
                            else:
                                prov_block["models"].pop(prov, None)
                    prov_ov = (prov_block.get("model_overrides") or {}).get(prov)
                    if isinstance(prov_ov, dict):
                        for ov_id in list(prov_ov.keys()):
                            if norm_id(str(ov_id)) in card_tokens:
                                prov_ov.pop(ov_id, None)
                        if not prov_ov:
                            prov_block["model_overrides"].pop(prov, None)
                    # If a factory baseline card still carries cash pricing on this provider,
                    # record an explicit null-pricing override so compile removes it.
                    if not removed_team_card and existing_card is not None:
                        prov_block.setdefault("model_overrides", {}).setdefault(prov, {})[
                            str(existing_card.get("id") or card_id)
                        ] = {"pricing": None}
            elif data.pricing is not None or data.pricing_meta is not None:
                card_id = str(merged_entry.get("model_id") or key)
                card_patch: dict[str, Any] = {}
                if data.pricing is not None:
                    raw_p = data.pricing.model_dump(exclude_unset=True)
                    pm_extra: dict[str, Any] = {}
                    for mf in ("source_kind", "estimated", "source", "source_url", "verified_at", "note"):
                        if mf in raw_p:
                            pm_extra[mf] = raw_p.pop(mf)
                    if "cache_read" in raw_p:
                        cr_val = raw_p.pop("cache_read")
                        if raw_p.get("cached_input") is None and cr_val is not None:
                            raw_p["cached_input"] = cr_val
                    card_patch["pricing"] = raw_p
                    if pm_extra:
                        card_patch["pricing_meta"] = pm_extra
                if data.pricing_meta is not None:
                    card_patch.setdefault("pricing_meta", {}).update(
                        data.pricing_meta.model_dump(exclude_unset=True)
                    )
                # Update team-added card in place if present, else write to providers.model_overrides
                prov_block = next_payload.setdefault("providers", {})
                team_cards = (prov_block.get("models") or {}).get(prov)
                updated_in_place = False
                if isinstance(team_cards, list):
                    for tc in team_cards:
                        if isinstance(tc, dict) and norm_id(str(tc.get("id"))) == norm_id(card_id):
                            deep_merge_patch_with_null_delete(tc, card_patch)
                            if isinstance(tc.get("pricing"), Mapping):
                                tc["pricing"] = normalize_cash_pricing_card(tc["pricing"]) or {}
                            updated_in_place = True
                            break
                if not updated_in_place:
                    if existing_card is not None:
                        target_card_ov = (
                            prov_block.setdefault("model_overrides", {})
                            .setdefault(prov, {})
                            .setdefault(str(existing_card.get("id") or card_id), {})
                        )
                        _merge_model_override_patch(target_card_ov, card_patch)
                    else:
                        new_card = _build_pricing_card_dict(
                            card_id, str(merged_entry.get("display_name") or key), data
                        )
                        if new_card is not None:
                            prov_block.setdefault("models", {}).setdefault(prov, []).append(new_card)

            if dm_patch is not None:
                cur_dm = next_payload.setdefault("dalton_meta", {}).setdefault(key, {})
                _merge_model_override_patch(cur_dm, dm_patch)

            if "task_costs_bound" in data.model_fields_set:
                bounds_map = next_payload.setdefault("task_costs", {}).setdefault("bounds", {})
                if data.task_costs_bound is None:
                    bounds_map[key] = None
                elif isinstance(data.task_costs_bound, Mapping):
                    existing_bound: dict[str, Any] = {}
                    raw_existing_bound = bounds_map.get(key)
                    if key not in bounds_map or raw_existing_bound is not None:
                        compiled_bound = (
                            (current_compiled.task_costs.bounds or {}).get(key)
                            if current_compiled.task_costs is not None
                            else None
                        )
                        if compiled_bound is not None:
                            existing_bound = {
                                "model": str(compiled_bound[0]),
                                "times": float(compiled_bound[1]),
                            }
                        if isinstance(raw_existing_bound, Mapping):
                            deep_merge_patch_with_null_delete(existing_bound, raw_existing_bound)
                    deep_merge_patch_with_null_delete(existing_bound, data.task_costs_bound)
                    if not existing_bound:
                        bounds_map[key] = None
                    else:
                        bound_model = existing_bound.get("model")
                        bound_times = existing_bound.get("times")
                        if (
                            not isinstance(bound_model, str)
                            or not bound_model.strip()
                            or isinstance(bound_times, bool)
                            or not isinstance(bound_times, (int, float))
                            or not math.isfinite(float(bound_times))
                            or float(bound_times) <= 0.0
                        ):
                            raise CatalogValidationError(
                                "invalid_task_costs_bound",
                                "task_costs_bound requires a non-empty 'model' and a positive finite 'times' multiplier.",
                            )
                        existing_bound["model"] = bound_model.strip()
                        existing_bound["times"] = float(bound_times)
                        bounds_map[key] = existing_bound

            curated_updated: list[str] | None = None
            if data.curated_setup_member is not None:
                curated_updated = _apply_curated_setup_member_in_tx(
                    next_payload,
                    current_compiled=current_compiled,
                    model_key=key,
                    include=bool(data.curated_setup_member),
                )

            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            diff_summary: dict[str, Any] = {"updated": key, "fields": sorted(patch.keys())}
            if variant_diff is not None:
                diff_summary["provider_variant"] = variant_diff
            if curated_updated is not None:
                diff_summary["curated_setup"] = curated_updated
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="update_model",
                target_keys=[key],
                payload=next_payload,
                diff_summary=diff_summary,
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        "model_key": key,
        "revision_record": saved["revision_record"],
    }


async def set_catalog_model_visibility(
    *,
    actor_user_id: str,
    model_key: str,
    data: ModelVisibilityInput,
) -> dict[str, Any]:
    """Hide or unhide a model (keeps it in ``ModelConfig`` so existing threads/automations run)."""
    key = model_key.strip()
    action = "unhide_model" if data.visible else "hide_model"
    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )
            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            if current_compiled.model_config.get_model_config(key) is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "model_not_found", "message": f"Model {key!r} not found."},
                )
            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA
            if key in (next_payload.get("models") or {}):
                next_payload["models"][key]["visible"] = bool(data.visible)
            else:
                next_payload.setdefault("model_overrides", {}).setdefault(key, {})["visible"] = bool(
                    data.visible
                )
            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action=action,
                target_keys=[key],
                payload=next_payload,
                diff_summary={"model_key": key, "visible": bool(data.visible)},
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        "model_key": key,
        "visible": bool(data.visible),
        "revision_record": saved["revision_record"],
    }


async def restore_factory_default_model(
    *,
    actor_user_id: str,
    model_key: str,
    expected_revision: int | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    """Remove DB overrides for a built-in/factory model so it returns to its shipped definition (§7.1)."""
    key = model_key.strip()
    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if expected_revision is not None and cur_rev != int(expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": expected_revision, "current_revision": cur_rev},
                )
            cur_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            factory_compiled = compile_catalog_from_payload(
                empty_catalog_payload(),
                revision=0,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            factory_entry = factory_compiled.model_config.get_model_config(key)
            if factory_entry is None:
                raise CatalogValidationError(
                    "not_a_factory_model",
                    f"Model {key!r} has no factory default definition and cannot be restored to factory default.",
                )
            cur_entry = cur_compiled.model_config.get_model_config(key)

            candidate_providers: set[str] = set()
            candidate_raw_ids: set[str] = {key}
            candidate_norm_ids: set[str] = {norm_id(key)}
            for mc_obj, entry_obj in (
                (factory_compiled.model_config, factory_entry),
                (cur_compiled.model_config, cur_entry),
            ):
                if not isinstance(entry_obj, Mapping):
                    continue
                p = str(entry_obj.get("provider") or "").strip()
                mid = str(entry_obj.get("model_id") or key).strip()
                if p:
                    candidate_providers.add(p)
                if mid:
                    candidate_raw_ids.add(mid)
                    candidate_norm_ids.add(norm_id(mid))
                if p and mid:
                    card_info = mc_obj.get_model_info(p, mid)
                    if isinstance(card_info, Mapping):
                        cid = str(card_info.get("id") or "").strip()
                        if cid:
                            candidate_raw_ids.add(cid)
                            candidate_norm_ids.add(norm_id(cid))
                        for alias in card_info.get("alias") or []:
                            astr = str(alias or "").strip()
                            if astr:
                                candidate_raw_ids.add(astr)
                                candidate_norm_ids.add(norm_id(astr))

            next_payload = copy.deepcopy(row["payload"])
            key_lower = key.lower()
            for section_name in ("model_overrides", "dalton_meta"):
                sec = next_payload.get(section_name)
                if isinstance(sec, dict):
                    for k_exist in [k for k in sec if str(k).lower() == key_lower]:
                        sec.pop(k_exist, None)
            tc_bounds = (next_payload.get("task_costs") or {}).get("bounds")
            if isinstance(tc_bounds, dict):
                for k_exist in [k for k in tc_bounds if str(k).lower() == key_lower]:
                    tc_bounds.pop(k_exist, None)

            # If a factory model had a promoted entry in payload.models, remove it so the
            # factory baseline definition takes effect again.
            if factory_entry is not None and isinstance(next_payload.get("models"), dict):
                for k_exist in [k for k in next_payload["models"] if str(k).lower() == key_lower]:
                    next_payload["models"].pop(k_exist, None)

            # Clear pricing card patches in providers.model_overrides[prov] and any factory-shadow
            # cards in providers.models[prov] (unless shared with another remaining team model).
            prov_block = next_payload.get("providers")
            if isinstance(prov_block, dict) and candidate_providers:
                remaining_team_cards: dict[str, set[str]] = {}
                for m_k, m_v in (next_payload.get("models") or {}).items():
                    if str(m_k).lower() == key_lower or not isinstance(m_v, Mapping):
                        continue
                    mp = str(m_v.get("provider") or "").strip()
                    mmid = str(m_v.get("model_id") or m_k).strip()
                    if mp and mmid:
                        remaining_team_cards.setdefault(mp, set()).add(norm_id(mmid))

                prov_ov = prov_block.get("model_overrides")
                if isinstance(prov_ov, dict):
                    for p in candidate_providers:
                        p_ov = prov_ov.get(p)
                        if not isinstance(p_ov, dict):
                            continue
                        shared_norms = remaining_team_cards.get(p, set())
                        for ov_k in list(p_ov.keys()):
                            ov_norm = norm_id(str(ov_k))
                            if (
                                str(ov_k) in candidate_raw_ids or ov_norm in candidate_norm_ids
                            ) and ov_norm not in shared_norms:
                                p_ov.pop(ov_k, None)
                        if not p_ov:
                            prov_ov.pop(p, None)

                prov_models = prov_block.get("models")
                if isinstance(prov_models, dict) and factory_entry is not None:
                    for p in candidate_providers:
                        cards = prov_models.get(p)
                        if not isinstance(cards, list):
                            continue
                        shared_norms = remaining_team_cards.get(p, set())
                        kept = [
                            c
                            for c in cards
                            if not (
                                isinstance(c, Mapping)
                                and norm_id(str(c.get("id") or "")) in candidate_norm_ids
                                and norm_id(str(c.get("id") or "")) not in shared_norms
                            )
                        ]
                        if kept:
                            prov_models[p] = kept
                        else:
                            prov_models.pop(p, None)

            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="update_model",
                target_keys=[key],
                payload=next_payload,
                diff_summary={"restored_factory_default": key},
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=comment or f"Restored factory default for {key}",
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {"revision": new_rev, "model_key": key, "revision_record": saved["revision_record"]}


# ---------------------------------------------------------------------------
# Transactional retire & restore (§2.7)
# ---------------------------------------------------------------------------


async def _validate_successor_for_owner(
    conn: Any,
    *,
    owner_id: str,
    successor: str,
    post_compiled: CompiledCatalog,
    owner_pref: Mapping[str, Any] | None = None,
) -> None:
    """Verify ``successor`` satisfies ``owner_id``'s account model policy & credential availability."""
    from src.dalton.credentials import credential_user_id, uses_shared_team_credential
    from src.dalton.database.team_model_catalog import get_catalog_provider_credential
    from src.llms.claude_code import is_claude_code_model
    from src.llms.llm import platform_key_configured
    from src.server.services.account import own_access
    from src.server.services.llm.byok import walk_byok_candidates
    from src.server.services.platform_client import platform_gating_active
    from src.server.services.policy import OWN_SUBSCRIPTIONS

    mc = post_compiled.model_config
    entry = mc.get_model_config(successor)
    if not isinstance(entry, Mapping):
        raise CatalogValidationError(
            "invalid_successor",
            f"successor_model {successor!r} is not active in the target catalog.",
        )

    provider = str(entry.get("provider") or "").strip()
    prov_info = mc.get_provider_info(provider) if provider else {}

    # 1. Account model policy (organization allow-list / role policy)
    own = await own_access(owner_id, sign_ins=False)
    policy = getattr(own, "policy", None)
    if policy is not None and not policy.allows_model(successor):
        raise CatalogConflictError(
            "successor_not_allowed_for_owner",
            f"successor_model {successor!r} is not permitted by account policy for user {owner_id!r}.",
            extra={"owner_user_id": owner_id, "successor_model": successor},
        )

    # 2. Credential availability for the successor's provider
    if is_claude_code_model(successor):
        return

    if prov_info.get("catalog_defined"):
        cred = await get_catalog_provider_credential(provider, conn=conn, decrypt=False)
        target_canonical = canonical_url_key(str(prov_info.get("base_url") or ""))
        if (
            not cred
            or not target_canonical
            or str(cred.get("canonical_bound_url") or "") != target_canonical
        ):
            raise CatalogConflictError(
                "successor_credential_unavailable",
                f"successor_model {successor!r} uses catalog variant {provider!r} without a matching bound credential.",
                extra={
                    "owner_user_id": owner_id,
                    "successor_model": successor,
                    "provider": provider,
                },
            )
        return

    access_type = str(prov_info.get("access_type") or "api_key")
    has_own_credential = False

    if access_type == "oauth":
        if policy is not None and not policy.allows(OWN_SUBSCRIPTIONS):
            raise CatalogConflictError(
                "successor_not_allowed_for_owner",
                f"Account policy for user {owner_id!r} disables OAuth subscriptions required by {successor!r}.",
                extra={"owner_user_id": owner_id, "successor_model": successor, "provider": provider},
            )
        from src.server.database.oauth_tokens import get_oauth_connection

        cred_uid = credential_user_id(owner_id) if uses_shared_team_credential(owner_id) else owner_id
        oauth_row = await get_oauth_connection(cred_uid, provider)
        if not oauth_row or oauth_row.get("needs_reauth_at"):
            raise CatalogConflictError(
                "successor_credential_unavailable",
                f"User {owner_id!r} has no active {provider!r} OAuth connection for successor_model {successor!r}.",
                extra={"owner_user_id": owner_id, "successor_model": successor, "provider": provider},
            )
        allowed_plans = {str(p).lower() for p in (entry.get("oauth_plans") or ()) if p}
        user_plan = str(oauth_row.get("plan_type") or "").lower()
        if allowed_plans and user_plan and user_plan not in allowed_plans:
            raise CatalogConflictError(
                "successor_not_allowed_for_owner",
                f"User {owner_id!r}'s {provider!r} plan ({user_plan!r}) does not cover successor_model {successor!r}.",
                extra={"owner_user_id": owner_id, "successor_model": successor, "provider": provider},
            )
        has_own_credential = True
    else:
        has_platform_key = bool(platform_key_configured(successor))
        byok_allowed = bool(getattr(own, "api_keys", True)) or uses_shared_team_credential(owner_id)
        if byok_allowed:
            found_key, _ = await walk_byok_candidates(owner_id, provider, mc)
            has_own_credential = found_key is not None
        if not has_platform_key and not has_own_credential:
            raise CatalogConflictError(
                "successor_credential_unavailable",
                f"User {owner_id!r} has no available API key for provider {provider!r} (successor_model {successor!r}).",
                extra={"owner_user_id": owner_id, "successor_model": successor, "provider": provider},
            )

    # 3. Platform tier / plan gating (when platform gating is active and no own credential overrides it)
    if platform_gating_active() and not has_own_credential:
        from src.server.services.llm.capabilities import PlanTier

        permits = PlanTier(owner_id)
        tier = entry.get("tier")
        if not (isinstance(tier, int) and await permits(tier)):
            raise CatalogConflictError(
                "successor_not_allowed_for_owner",
                f"User {owner_id!r}'s plan does not include successor_model {successor!r}.",
                extra={"owner_user_id": owner_id, "successor_model": successor},
            )


async def _update_automation_model_with_history_in_tx(
    conn: Any,
    *,
    locked_row: Mapping[str, Any],
    target_model: str | None,
    target_effort: str | None,
    actor_user_id: str | None,
    pref_row: Mapping[str, Any] | None,
    now_iso: str,
) -> dict[str, Any] | None:
    """Update an already-locked automation's model/effort while maintaining ``audit_history`` and ``active_version``."""
    from src.dalton.database.company_automations import prepare_update_history_columns

    update_fields = {
        "llm_model": target_model,
        "reasoning_effort": target_effort,
    }
    extra_hist_cols, _ = await prepare_update_history_columns(
        locked_row,
        update_fields,
        actor_user_id=actor_user_id,
        action="update",
        skip_if_unchanged=False,
        pref_row=pref_row,
        changed_at_iso=now_iso,
        conn=conn,
    )
    new_cv = int(extra_hist_cols["config_version"])
    new_av = int(extra_hist_cols["active_version"])
    new_history = extra_hist_cols["audit_history"]
    eff_updated_by = extra_hist_cols.get("updated_by", actor_user_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE automations
            SET llm_model = %s,
                reasoning_effort = %s,
                config_version = %s,
                active_version = %s,
                updated_by = %s,
                audit_history = %s,
                updated_at = NOW()
            WHERE automation_id = %s::uuid
              AND deleted_at IS NULL
            RETURNING config_version, active_version, updated_at
            """,
            (
                target_model,
                target_effort,
                new_cv,
                new_av,
                eff_updated_by,
                Json(new_history),
                str(locked_row["automation_id"]),
            ),
        )
        return await cur.fetchone()


async def _validate_and_replace_references_in_tx(
    conn: Any,
    *,
    retired_key: str,
    successor_model: str | None,
    refs: dict[str, Any],
    post_compiled: CompiledCatalog,
    migrate_user_defaults: bool = False,
    clear_subagent_model: bool = False,
    actor_user_id: str | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate hard blocks and perform transactional batch replacement with CAS side_effects."""
    if refs["agent_config"] or refs["dalton_settings"]:
        raise CatalogConflictError(
            "model_in_use_by_config",
            f"Model {retired_key!r} is referenced by read-only deployment config and cannot be retired.",
            extra={
                "agent_config": refs["agent_config"],
                "dalton_settings": refs["dalton_settings"],
            },
        )
    if refs["bundled_plugin_subagents"]:
        raise CatalogConflictError(
            "model_in_use_by_bundle",
            f"Model {retired_key!r} is pinned by a bundled plugin agent file and cannot be retired.",
            extra={"bundled_plugin_subagents": refs["bundled_plugin_subagents"]},
        )

    successor = (
        str(successor_model).strip()
        if successor_model is not None and str(successor_model).strip()
        else None
    )
    if clear_subagent_model and successor:
        raise CatalogValidationError(
            "conflicting_retire_options",
            "clear_subagent_model=true cannot be combined with successor_model.",
        )

    need_automation_replace = bool(refs["automations"])
    need_subagent_replace = bool(refs["user_subagents"] or refs["plugin_subagents"])

    if (need_automation_replace or (need_subagent_replace and not clear_subagent_model)) and not successor:
        raise CatalogConflictError(
            "model_in_use",
            f"Model {retired_key!r} is still referenced by active automations or subagents; "
            "specify successor_model to replace them in the same transaction.",
            extra={
                "automations": refs["automations"],
                "user_subagents": refs["user_subagents"],
                "plugin_subagents": refs["plugin_subagents"],
                "user_preferences": [
                    {"user_id": u["user_id"], "slots": u["slots"]}
                    for u in refs["user_preferences"]
                ],
            },
        )

    if successor:
        if successor == retired_key:
            raise CatalogValidationError(
                "invalid_successor", "successor_model cannot be the model being retired."
            )
        if post_compiled.model_config.get_model_config(successor) is None:
            raise CatalogValidationError(
                "invalid_successor",
                f"successor_model {successor!r} is not active in the target catalog.",
            )

    # Single shared replacement plan used by BOTH owner validation and transactional writes.
    subagent_target_model: str | None = None if clear_subagent_model else successor
    replace_automations_with_successor = bool(successor and refs["automations"])
    replace_subagents_with_successor = bool(
        subagent_target_model and (refs["user_subagents"] or refs["plugin_subagents"])
    )
    replace_user_prefs_with_successor = bool(
        migrate_user_defaults and successor and refs["user_preferences"]
    )

    from src.llms.model_spec import clamp_reasoning_effort, reasoning_block
    from src.server.database import automation as auto_db
    from src.server.database.user import lock_user_preferences
    from src.server.database.user_subagents import lock_user_subagents
    from src.server.services.automations.lifecycle import check_claude_code_hosted

    side_effects: list[dict[str, Any]] = []
    affected_pref_users: set[str] = set()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Canonical lock order (strictly AFTER catalog exclusive lock, matching automation.py):
    #   1. user_preferences (sorted user_id) -> 2. automations (sorted user_id, automation_id) -> 3. user_subagents (sorted user_id)
    pref_lock_users: set[str] = {str(a["user_id"]) for a in refs["automations"]}
    if replace_user_prefs_with_successor:
        pref_lock_users.update(str(u["user_id"]) for u in refs["user_preferences"])

    locked_prefs: dict[str, dict[str, Any]] = {}
    for uid in sorted(pref_lock_users):
        prow = await lock_user_preferences(uid, conn=conn)
        if isinstance(prow, Mapping):
            locked_prefs[uid] = dict(prow)

    # Per-owner account model policy & credential availability check for successor_model
    if successor:
        owners_receiving_successor: set[str] = set()
        if replace_automations_with_successor:
            owners_receiving_successor.update(str(a["user_id"]) for a in refs["automations"])
        if replace_subagents_with_successor:
            owners_receiving_successor.update(str(r["user_id"]) for r in refs["user_subagents"])
            owners_receiving_successor.update(str(r["user_id"]) for r in refs["plugin_subagents"])
        if replace_user_prefs_with_successor:
            owners_receiving_successor.update(str(u["user_id"]) for u in refs["user_preferences"])
        for uid in sorted(owners_receiving_successor):
            await _validate_successor_for_owner(
                conn,
                owner_id=uid,
                successor=successor,
                post_compiled=post_compiled,
                owner_pref=locked_prefs.get(uid),
            )

    # 1. Optional one-click migration of user_preferences default slots (FIRST, while user_preferences is locked, BEFORE automations)
    if replace_user_prefs_with_successor:
        successor_entry = post_compiled.model_config.get_model_config(successor) or {}
        rb = reasoning_block(successor_entry)
        efforts = rb.get("efforts") or []
        default_eff = rb.get("default")
        for up in sorted(refs["user_preferences"], key=lambda item: str(item["user_id"])):
            uid = str(up["user_id"])
            live_pref = locked_prefs.get(uid) or up
            mp = copy.deepcopy(live_pref.get("model_preference") or {})
            ap = copy.deepcopy(live_pref.get("agent_preference") or {})
            before_vals: dict[str, Any] = {}
            after_vals: dict[str, Any] = {}
            for sk in _USER_PREF_SLOT_KEYS:
                if mp.get(sk) == retired_key:
                    before_vals[f"model_preference.{sk}"] = retired_key
                    mp[sk] = successor
                    after_vals[f"model_preference.{sk}"] = successor
            profiles = mp.get("profiles")
            if isinstance(profiles, dict) and isinstance(profiles.get(successor), dict):
                raw_eff = profiles[successor].get("reasoning_effort")
                if raw_eff:
                    profiles[successor]["reasoning_effort"] = (
                        clamp_reasoning_effort(efforts, default_eff, raw_eff) if efforts else None
                    )
            sub_models = ap.get("subagent_models")
            if isinstance(sub_models, dict):
                for sname, smodel in list(sub_models.items()):
                    if smodel == retired_key:
                        before_vals[f"agent_preference.subagent_models.{sname}"] = retired_key
                        sub_models[sname] = successor
                        after_vals[f"agent_preference.subagent_models.{sname}"] = successor
            if before_vals:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        UPDATE user_preferences
                        SET model_preference = %s,
                            agent_preference = %s,
                            updated_at = NOW()
                        WHERE user_id = %s
                        RETURNING updated_at
                        """,
                        (Json(mp), Json(ap), uid),
                    )
                    upd = await cur.fetchone()
                    if upd:
                        affected_pref_users.add(uid)
                        if uid in locked_prefs:
                            locked_prefs[uid]["model_preference"] = mp
                            locked_prefs[uid]["agent_preference"] = ap
                        side_effects.append(
                            {
                                "kind": "user_preference",
                                "id": uid,
                                "user_id": uid,
                                "before": before_vals,
                                "after": after_vals,
                                "before_updated_at": _ts_str(live_pref.get("updated_at") or up.get("updated_at")),
                                "after_updated_at": _ts_str(upd["updated_at"]),
                            }
                        )

    # 2. Automations: lock in sorted user_id order (strictly AFTER catalog exclusive lock and user_preferences lock)
    if refs["automations"]:
        assert successor is not None
        successor_entry = post_compiled.model_config.get_model_config(successor) or {}
        rb = reasoning_block(successor_entry)
        efforts = rb.get("efforts") or []
        default_eff = rb.get("default")

        for owner_id in sorted({str(a["user_id"]) for a in refs["automations"]}):
            await auto_db.lock_user_automations(owner_id, conn=conn)

        for auto in sorted(refs["automations"], key=lambda a: (str(a["user_id"]), str(a["automation_id"]))):
            owner_id = str(auto["user_id"])
            ws_id = auto.get("workspace_id")
            # Per-owner Claude Code hosted workspace constraint check
            await check_claude_code_hosted(owner_id, successor, ws_id, conn, None)

            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {auto_db.AUTOMATION_COLUMNS}, audit_history
                    FROM automations
                    WHERE automation_id = %s::uuid
                      AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (auto["automation_id"],),
                )
                locked_row = await cur.fetchone()
            if not locked_row:
                continue

            old_effort = locked_row.get("reasoning_effort")
            if old_effort:
                new_effort = clamp_reasoning_effort(efforts, default_eff, old_effort) if efforts else None
            else:
                new_effort = None

            updated = await _update_automation_model_with_history_in_tx(
                conn,
                locked_row=locked_row,
                target_model=successor,
                target_effort=new_effort,
                actor_user_id=actor_user_id,
                pref_row=locked_prefs.get(owner_id),
                now_iso=now_iso,
            )
            if updated:
                side_effects.append(
                    {
                        "kind": "automation",
                        "id": str(locked_row["automation_id"]),
                        "user_id": owner_id,
                        "before": {
                            "llm_model": locked_row["llm_model"],
                            "reasoning_effort": old_effort,
                        },
                        "after": {
                            "llm_model": successor,
                            "reasoning_effort": new_effort,
                        },
                        "before_config_version": int(locked_row["config_version"]),
                        "after_config_version": int(updated["config_version"]),
                    }
                )

    # 3. User subagents & plugin subagents (lock user_subagents per user in sorted order)
    sub_users = sorted(
        {str(r["user_id"]) for r in refs["user_subagents"]}
        | {str(r["user_id"]) for r in refs["plugin_subagents"]}
    )
    for uid in sub_users:
        await lock_user_subagents(uid, conn=conn)

    for us in refs["user_subagents"]:
        new_fields = copy.deepcopy(us["fields"])
        if subagent_target_model is None:
            new_fields.pop("model", None)
            after_model = None
        else:
            new_fields["model"] = subagent_target_model
            after_model = subagent_target_model
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE user_subagents
                SET fields = %s, updated_at = NOW()
                WHERE user_subagent_id = %s::uuid
                RETURNING updated_at
                """,
                (Json(new_fields), us["user_subagent_id"]),
            )
            upd = await cur.fetchone()
            if upd:
                side_effects.append(
                    {
                        "kind": "user_subagent",
                        "id": us["user_subagent_id"],
                        "user_id": us["user_id"],
                        "before": {"model": us["model"]},
                        "after": {"model": after_model},
                        "before_updated_at": us["updated_at"],
                        "after_updated_at": _ts_str(upd["updated_at"]),
                    }
                )

    for ps in refs["plugin_subagents"]:
        new_fields = copy.deepcopy(ps["fields"])
        if subagent_target_model is None:
            new_fields.pop("model", None)
            after_model = None
        else:
            new_fields["model"] = subagent_target_model
            after_model = subagent_target_model
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE plugin_subagents
                SET fields = %s, updated_at = NOW()
                WHERE plugin_subagent_id = %s::uuid
                RETURNING updated_at
                """,
                (Json(new_fields), ps["plugin_subagent_id"]),
            )
            upd = await cur.fetchone()
            if upd:
                side_effects.append(
                    {
                        "kind": "plugin_subagent",
                        "id": ps["plugin_subagent_id"],
                        "user_id": ps["user_id"],
                        "before": {"model": ps["model"]},
                        "after": {"model": after_model},
                        "before_updated_at": ps["updated_at"],
                        "after_updated_at": _ts_str(upd["updated_at"]),
                    }
                )

    return side_effects, affected_pref_users


async def retire_catalog_model(
    *,
    actor_user_id: str,
    model_key: str,
    data: ModelRetireInput,
) -> dict[str, Any]:
    """Retire ``model_key`` under exclusive catalog advisory lock with 6-source check & batch replacement."""
    key = model_key.strip()
    affected_pref_users: set[str] = set()

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )

            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            existing_entry = current_compiled.model_config.get_model_config(key)
            if existing_entry is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "model_not_found", "message": f"Model {key!r} is not active."},
                )

            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA
            retired_list = list(next_payload.get("retired") or [])
            if not any(isinstance(r, Mapping) and r.get("key") == key for r in retired_list):
                retired_list.append(
                    {
                        "key": key,
                        "provider": str(existing_entry.get("provider") or ""),
                        "retired_at": _utcnow_iso(),
                        "retired_by": actor_user_id,
                    }
                )
            next_payload["retired"] = retired_list

            post_compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )

            try:
                refs = await collect_classified_model_references({key}, conn=conn)
            except ReferenceSourceUnavailable as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "reference_source_unavailable",
                        "source": exc.source,
                        "message": f"Reference check failed ({exc.source}); retire refused.",
                    },
                ) from exc

            side_effects, affected_pref_users = await _validate_and_replace_references_in_tx(
                conn,
                retired_key=key,
                successor_model=data.successor_model,
                refs=refs,
                post_compiled=post_compiled,
                migrate_user_defaults=data.migrate_user_defaults,
                clear_subagent_model=data.clear_subagent_model,
                actor_user_id=actor_user_id,
            )

            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="retire_model",
                target_keys=[key],
                payload=next_payload,
                diff_summary={
                    "retired": key,
                    "successor_model": data.successor_model,
                    "replaced_count": len(side_effects),
                },
                side_effects=side_effects,
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(post_compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=post_compiled)
    if affected_pref_users:
        from src.server.database.user import invalidate_user_prefs_cache

        for uid in affected_pref_users:
            try:
                await invalidate_user_prefs_cache(uid)
            except Exception:
                pass
    return {
        "revision": new_rev,
        "model_key": key,
        "side_effects": side_effects,
        "revision_record": saved["revision_record"],
    }


async def restore_catalog_model(
    *,
    actor_user_id: str,
    model_key: str,
    data: ModelRestoreInput,
) -> dict[str, Any]:
    """Restore a team-retired model (refuses factory retired 400 and readonly legacy retired 409)."""
    key = model_key.strip()
    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )

            from src.dalton.database.team_model_catalog import fetch_legacy_retired_history
            from src.dalton.services.team_model_catalog.legacy_import import (
                extract_disk_legacy_retired_readonly,
            )

            extra_readonly: set[str] = set()
            if row["legacy_sync_import_status"] == "failed":
                extra_readonly |= extract_disk_legacy_retired_readonly()
                try:
                    extra_readonly |= await fetch_legacy_retired_history(conn=conn)
                except Exception:
                    pass

            current_compiled = compile_catalog_from_payload(
                row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
                extra_readonly_legacy_retired=extra_readonly,
            )
            if key in current_compiled.factory_retired_keys:
                raise CatalogValidationError(
                    "factory_retired_immutable",
                    f"Model {key!r} is retired in the factory baseline and cannot be restored.",
                )
            if key in current_compiled.readonly_legacy_retired_keys:
                raise CatalogConflictError(
                    "legacy_retired_locked_until_import_resolved",
                    f"Model {key!r} is locked in the read-only legacy retired set until "
                    "retry_import or abandon_legacy_file resolves the failed legacy import.",
                )

            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA
            retired_list = list(next_payload.get("retired") or [])
            remaining = [
                r for r in retired_list if not (isinstance(r, Mapping) and r.get("key") == key)
            ]
            if len(remaining) == len(retired_list):
                raise HTTPException(
                    status_code=404,
                    detail={"code": "model_not_retired", "message": f"Model {key!r} is not retired."},
                )
            next_payload["retired"] = remaining

            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="restore_model",
                target_keys=[key],
                payload=next_payload,
                diff_summary={"restored": key},
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {"revision": new_rev, "model_key": key, "revision_record": saved["revision_record"]}


# ---------------------------------------------------------------------------
# Curated setup update
# ---------------------------------------------------------------------------


async def update_curated_setup(
    *,
    actor_user_id: str,
    data: CuratedSetupUpdateInput,
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            row = await get_catalog_row(conn=conn, for_update=True)
            if row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )
            next_payload = copy.deepcopy(row["payload"])
            next_payload["schema"] = CATALOG_SCHEMA
            next_payload["curated_setup"] = (
                [str(k).strip() for k in data.curated_setup if str(k).strip()]
                if data.curated_setup is not None
                else None
            )
            compiled = trial_compile_and_validate_catalog(
                next_payload,
                revision=cur_rev + 1,
                legacy_sync_import_status=row["legacy_sync_import_status"],
                last_known_legacy_retired=row["last_known_legacy_retired"],
            )
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="update_curated",
                target_keys=list(next_payload.get("curated_setup") or []),
                payload=next_payload,
                diff_summary={"curated_setup": next_payload["curated_setup"]},
                side_effects=[],
                actor_user_id=actor_user_id,
                comment=data.comment,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=compiled)
    return {
        "revision": new_rev,
        **derive_curated_setup_for_catalog(compiled),
        "revision_record": saved["revision_record"],
    }


# ---------------------------------------------------------------------------
# 4-Step Safe Rollback with chained A -> B -> C atomic restoration (§3.4)
# ---------------------------------------------------------------------------


def resolve_chained_side_effect_target(
    chain_newest_first: list[dict[str, Any]],
    *,
    current_version: Any,
    current_value: Mapping[str, Any] | None = None,
    version_kind: str = "config_version",
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the atomic restoration target across a contiguous chain ``[e_k, ..., e_1]``.

    - If ``current_version != e_k.after_<version_kind>`` (or for ``updated_at`` rows,
      any field in ``e_k.after`` drifted from ``current_value``): user edited the row
      after the last catalog replacement -> return ``(None, "user_modified_after_last_replacement")``.
    - Otherwise walk backward from ``e_k`` to ``e_1`` checking row-level version/timestamp
      continuity (and per-field continuity for overlapping or untouched fields across hops),
      then apply every contiguous hop's ``before`` map in reverse chronological order so
      successive catalog changes touching different fields of the same row (e.g.
      ``preferred_model`` then ``flash_model``) are all restored atomically.
    """
    if not chain_newest_first:
        return None, "empty_chain"
    after_key = f"after_{version_kind}"
    before_key = f"before_{version_kind}"

    newest = chain_newest_first[0]
    if version_kind == "config_version":
        if current_version is None or int(current_version) != int(newest.get(after_key, -1)):
            return None, "user_modified_after_last_replacement"
    else:
        if str(current_version or "") != str(newest.get(after_key) or ""):
            return None, "user_modified_after_last_replacement"
        if current_value is not None:
            newest_after = dict(newest.get("after") or {})
            for fk, expected_after in newest_after.items():
                if current_value.get(fk) != expected_after:
                    return None, "user_modified_after_last_replacement"

    # Walk backward across contiguous catalog rewrites (e.g. C <- B <- A, or multi-field pref hops)
    contiguous_newest_first: list[dict[str, Any]] = [newest]
    expected_before_by_field: dict[str, Any] = dict(newest.get("before") or {})
    for prev_hop in chain_newest_first[1:]:
        later_hop = contiguous_newest_first[-1]
        if version_kind == "config_version":
            contiguous = (
                later_hop.get(before_key) is not None
                and prev_hop.get(after_key) is not None
                and int(later_hop[before_key]) == int(prev_hop[after_key])
            )
        else:
            ts_contiguous = str(later_hop.get(before_key) or "") == str(
                prev_hop.get(after_key) or ""
            )
            if not ts_contiguous:
                contiguous = False
            else:
                prev_after = dict(prev_hop.get("after") or {})
                fields_ok = True
                for fk, prev_after_val in prev_after.items():
                    if fk in expected_before_by_field:
                        if expected_before_by_field[fk] != prev_after_val:
                            fields_ok = False
                            break
                    elif current_value is not None and fk in current_value:
                        if current_value[fk] != prev_after_val:
                            fields_ok = False
                            break
                contiguous = fields_ok
        if not contiguous:
            # User intervened between prev_hop and later_hop; stop at later_hop
            break
        contiguous_newest_first.append(prev_hop)
        for fk, old_val in (prev_hop.get("before") or {}).items():
            expected_before_by_field[fk] = old_val

    restored: dict[str, Any] = {}
    for hop in contiguous_newest_first:
        for fk, old_val in (hop.get("before") or {}).items():
            restored[fk] = old_val
    return restored, None


async def rollback_catalog(
    *,
    actor_user_id: str,
    data: CatalogRollbackInput,
) -> dict[str, Any]:
    """Roll back team catalog to ``target_revision = T`` following §3.4's 4-step order."""
    # Pre-lock network preflight for any newly supplied variant credentials
    for slug, cred_in in data.variant_credentials.items():
        try:
            await preflight_catalog_variant_url(cred_in.base_url)
        except (ValueError, EgressBlockedError) as exc:
            raise CatalogValidationError(
                "variant_base_url_blocked",
                f"Preflight failed for rollback credential '{slug}': {exc}",
            ) from exc

    restored_tasks: list[dict[str, Any]] = []
    skipped_user_modified: list[dict[str, Any]] = []
    affected_pref_users: set[str] = set()

    async with get_db_connection() as conn:
        async with conn.transaction():
            await acquire_catalog_exclusive_lock(conn)
            cur_row = await get_catalog_row(conn=conn, for_update=True)
            if cur_row is None:
                raise RuntimeError("dalton_team_model_catalog singleton row missing")
            cur_rev = int(cur_row["revision"])
            if data.expected_revision is not None and cur_rev != int(data.expected_revision):
                raise CatalogConflictError(
                    "revision_conflict",
                    f"Expected revision {data.expected_revision}, current is {cur_rev}.",
                    extra={"expected_revision": data.expected_revision, "current_revision": cur_rev},
                )
            if data.target_revision > cur_rev:
                raise CatalogValidationError(
                    "invalid_target_revision",
                    f"target_revision {data.target_revision} exceeds current revision {cur_rev}.",
                )
            target_row = await get_catalog_revision(data.target_revision, conn=conn)
            if target_row is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "revision_not_found",
                        "message": f"Catalog revision {data.target_revision} not found.",
                    },
                )

            cur_compiled = compile_catalog_from_payload(
                cur_row["payload"],
                revision=cur_rev,
                legacy_sync_import_status=cur_row["legacy_sync_import_status"],
                last_known_legacy_retired=cur_row["last_known_legacy_retired"],
            )

            # STEP 1: Target capability pruning + FULL base_url variant credential verification
            target_payload = copy.deepcopy(target_row["payload"])
            target_payload["schema"] = CATALOG_SCHEMA
            oauth_on = has_capability(CapabilityId.NATIVE_OAUTH_SYNC)
            target_compiled = trial_compile_and_validate_catalog(
                target_payload,
                revision=cur_rev + 1,
                oauth_sync_enabled=oauth_on,
                legacy_sync_import_status=cur_row["legacy_sync_import_status"],
                last_known_legacy_retired=cur_row["last_known_legacy_retired"],
            )

            existing_creds = await list_catalog_provider_credentials(conn=conn)
            missing_variant_creds: list[dict[str, Any]] = []
            creds_to_upsert: list[tuple[str, str, str]] = []
            for slug, vinfo in target_compiled.provider_variants.items():
                target_url = str(vinfo.get("base_url") or "").strip()
                target_canonical = canonical_url_key(target_url)
                cred = existing_creds.get(slug)
                matches_existing = (
                    cred is not None
                    and target_canonical is not None
                    and str(cred.get("canonical_bound_url") or "") == target_canonical
                    and endpoint_matches_prefix(target_url, str(cred.get("bound_base_url") or ""))
                )
                if matches_existing:
                    continue
                supplied = data.variant_credentials.get(slug)
                if (
                    supplied is not None
                    and target_canonical is not None
                    and canonical_url_key(supplied.base_url) == target_canonical
                    and endpoint_matches_prefix(target_url, supplied.base_url)
                ):
                    creds_to_upsert.append((slug, supplied.api_key.strip(), target_url))
                else:
                    missing_variant_creds.append(
                        {
                            "provider_slug": slug,
                            "required_base_url": target_url,
                            "current_bound_base_url": (cred or {}).get("bound_base_url"),
                        }
                    )

            if missing_variant_creds:
                raise CatalogConflictError(
                    "variant_credential_required",
                    "Rollback target contains catalog provider variant(s) whose full base_url "
                    "does not match any stored credential; provide matching variant_credentials to proceed.",
                    extra={"missing_variant_credentials": missing_variant_creds},
                )

            for slug, api_key, target_url in creds_to_upsert:
                await upsert_catalog_provider_credential(
                    conn,
                    provider_slug=slug,
                    api_key=api_key,
                    bound_base_url=target_url,
                    updated_by=actor_user_id,
                )

            # STEP 2: Atomic chained restoration across (T, C] side_effects
            intervening_revs = await list_revisions_between(
                data.target_revision, cur_rev, conn=conn
            )
            chains: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for rev_item in intervening_revs:
                for eff in rev_item.get("side_effects") or []:
                    if not isinstance(eff, Mapping):
                        continue
                    ekind = str(eff.get("kind") or "")
                    eid = str(eff.get("id") or "")
                    if ekind and eid:
                        chains.setdefault((ekind, eid), []).append(dict(eff))

            target_active_models = set((target_compiled.model_config.llm_config or {}).keys())
            rollback_side_effects: list[dict[str, Any]] = []
            now_iso = datetime.now(timezone.utc).isoformat()

            from src.server.database import automation as auto_db
            from src.server.database.user import lock_user_preferences
            from src.server.database.user_subagents import lock_user_subagents

            pref_chains = sorted(
                ((eid, hops) for (ekind, eid), hops in chains.items() if ekind == "user_preference"),
                key=lambda item: item[0],
            )
            auto_chains = sorted(
                ((eid, hops) for (ekind, eid), hops in chains.items() if ekind == "automation"),
                key=lambda item: item[0],
            )
            sub_chains = sorted(
                (
                    (ekind, eid, hops)
                    for (ekind, eid), hops in chains.items()
                    if ekind in ("user_subagent", "plugin_subagent")
                ),
                key=lambda item: (item[0], item[1]),
            )

            # Canonical lock order (strictly AFTER catalog exclusive lock, matching automation.py):
            #   1. user_preferences (sorted user_id) -> 2. automations (sorted user_id, automation_id) -> 3. user_subagents (sorted user_id)
            pref_lock_uids: set[str] = {eid for eid, _ in pref_chains}
            for _eid, hops in auto_chains:
                for hop in hops:
                    uid = str(hop.get("user_id") or "").strip()
                    if uid:
                        pref_lock_uids.add(uid)
            locked_prefs: dict[str, dict[str, Any]] = {}
            for uid in sorted(pref_lock_uids):
                prow = await lock_user_preferences(uid, conn=conn)
                if isinstance(prow, Mapping):
                    locked_prefs[uid] = dict(prow)

            # 1. Restore user_preferences FIRST (before touching automations)
            for eid, hops_oldest_first in pref_chains:
                hops_newest_first = list(reversed(hops_oldest_first))
                prow = locked_prefs.get(eid)
                if not prow:
                    continue
                mp = copy.deepcopy(prow.get("model_preference") or {})
                ap = copy.deepcopy(prow.get("agent_preference") or {})
                all_paths: set[str] = set()
                for hop in hops_newest_first:
                    all_paths.update((hop.get("after") or {}).keys())
                    all_paths.update((hop.get("before") or {}).keys())
                cur_vals: dict[str, Any] = {}
                for path in sorted(all_paths):
                    if path.startswith("model_preference."):
                        cur_vals[path] = mp.get(path.split(".", 1)[1])
                    elif path.startswith("agent_preference.subagent_models."):
                        sname = path.split(".", 2)[2]
                        cur_vals[path] = (ap.get("subagent_models") or {}).get(sname)
                restore_before, skip_reason = resolve_chained_side_effect_target(
                    hops_newest_first,
                    current_version=_ts_str(prow.get("updated_at")),
                    current_value=cur_vals,
                    version_kind="updated_at",
                )
                if restore_before is None:
                    skipped_user_modified.append(
                        {"kind": "user_preference", "id": eid, "user_id": eid, "reason": skip_reason}
                    )
                    continue
                actual_before: dict[str, Any] = {}
                actual_after: dict[str, Any] = {}
                for path, old_val in restore_before.items():
                    if old_val and old_val not in target_active_models:
                        continue
                    if path.startswith("model_preference."):
                        slot = path.split(".", 1)[1]
                        actual_before[path] = mp.get(slot)
                        mp[slot] = old_val
                        actual_after[path] = old_val
                    elif path.startswith("agent_preference.subagent_models."):
                        sname = path.split(".", 2)[2]
                        actual_before[path] = (ap.get("subagent_models") or {}).get(sname)
                        ap.setdefault("subagent_models", {})[sname] = old_val
                        actual_after[path] = old_val
                if not actual_after:
                    skipped_user_modified.append(
                        {
                            "kind": "user_preference",
                            "id": eid,
                            "user_id": eid,
                            "reason": "target_model_unavailable_in_target_revision",
                        }
                    )
                    continue
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        UPDATE user_preferences
                        SET model_preference = %s, agent_preference = %s, updated_at = NOW()
                        WHERE user_id = %s RETURNING updated_at
                        """,
                        (Json(mp), Json(ap), eid),
                    )
                    upd = await cur.fetchone()
                if upd:
                    affected_pref_users.add(eid)
                    locked_prefs[eid]["model_preference"] = mp
                    locked_prefs[eid]["agent_preference"] = ap
                    item = {
                        "kind": "user_preference",
                        "id": eid,
                        "user_id": eid,
                        "before": actual_before,
                        "after": actual_after,
                        "before_updated_at": _ts_str(prow.get("updated_at")),
                        "after_updated_at": _ts_str(upd["updated_at"]),
                    }
                    restored_tasks.append(item)
                    rollback_side_effects.append(item)

            # 2. Restore automations SECOND (with full audit_history & active_version maintenance)
            auto_owner_ids = sorted(
                {
                    str(hop.get("user_id") or "").strip()
                    for _eid, hops in auto_chains
                    for hop in hops
                    if str(hop.get("user_id") or "").strip()
                }
            )
            for owner_id in auto_owner_ids:
                await auto_db.lock_user_automations(owner_id, conn=conn)

            for eid, hops_oldest_first in auto_chains:
                hops_newest_first = list(reversed(hops_oldest_first))
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        SELECT {auto_db.AUTOMATION_COLUMNS}, audit_history
                        FROM automations
                        WHERE automation_id = %s::uuid AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (eid,),
                    )
                    arow = await cur.fetchone()
                if not arow:
                    continue
                restore_before, skip_reason = resolve_chained_side_effect_target(
                    hops_newest_first,
                    current_version=arow["config_version"],
                    version_kind="config_version",
                )
                if restore_before is None:
                    skipped_user_modified.append(
                        {
                            "kind": "automation",
                            "id": eid,
                            "user_id": str(arow["user_id"]),
                            "reason": skip_reason,
                        }
                    )
                    continue
                target_model = restore_before.get("llm_model")
                if target_model and target_model not in target_active_models:
                    skipped_user_modified.append(
                        {
                            "kind": "automation",
                            "id": eid,
                            "user_id": str(arow["user_id"]),
                            "reason": "target_model_unavailable_in_target_revision",
                        }
                    )
                    continue
                target_eff = restore_before.get("reasoning_effort")
                upd = await _update_automation_model_with_history_in_tx(
                    conn,
                    locked_row=arow,
                    target_model=target_model,
                    target_effort=target_eff,
                    actor_user_id=actor_user_id,
                    pref_row=locked_prefs.get(str(arow["user_id"])),
                    now_iso=now_iso,
                )
                if upd:
                    item = {
                        "kind": "automation",
                        "id": eid,
                        "user_id": str(arow["user_id"]),
                        "before": {
                            "llm_model": arow["llm_model"],
                            "reasoning_effort": arow["reasoning_effort"],
                        },
                        "after": {
                            "llm_model": target_model,
                            "reasoning_effort": target_eff,
                        },
                        "before_config_version": int(arow["config_version"]),
                        "after_config_version": int(upd["config_version"]),
                    }
                    restored_tasks.append(item)
                    rollback_side_effects.append(item)

            # 3. Restore user_subagents & plugin_subagents THIRD
            sub_owner_ids = sorted(
                {
                    str(hop.get("user_id") or "").strip()
                    for _ekind, _eid, hops in sub_chains
                    for hop in hops
                    if str(hop.get("user_id") or "").strip()
                }
            )
            for uid in sub_owner_ids:
                await lock_user_subagents(uid, conn=conn)

            for ekind, eid, hops_oldest_first in sub_chains:
                hops_newest_first = list(reversed(hops_oldest_first))
                tbl = "user_subagents" if ekind == "user_subagent" else "plugin_subagents"
                id_col = "user_subagent_id" if ekind == "user_subagent" else "plugin_subagent_id"
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT {id_col}::text AS id, user_id, fields, updated_at "
                        f"FROM {tbl} WHERE {id_col} = %s::uuid FOR UPDATE",
                        (eid,),
                    )
                    srow = await cur.fetchone()
                if not srow:
                    continue
                cur_fields = dict(srow.get("fields") or {})
                restore_before, skip_reason = resolve_chained_side_effect_target(
                    hops_newest_first,
                    current_version=_ts_str(srow.get("updated_at")),
                    current_value={"model": cur_fields.get("model")},
                    version_kind="updated_at",
                )
                if restore_before is None:
                    skipped_user_modified.append(
                        {"kind": ekind, "id": eid, "user_id": str(srow["user_id"]), "reason": skip_reason}
                    )
                    continue
                target_model = restore_before.get("model")
                if target_model and target_model not in target_active_models:
                    skipped_user_modified.append(
                        {
                            "kind": ekind,
                            "id": eid,
                            "user_id": str(srow["user_id"]),
                            "reason": "target_model_unavailable_in_target_revision",
                        }
                    )
                    continue
                new_fields = copy.deepcopy(cur_fields)
                if target_model is None:
                    new_fields.pop("model", None)
                else:
                    new_fields["model"] = target_model
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"UPDATE {tbl} SET fields = %s, updated_at = NOW() "
                        f"WHERE {id_col} = %s::uuid RETURNING updated_at",
                        (Json(new_fields), eid),
                    )
                    upd = await cur.fetchone()
                if upd:
                    item = {
                        "kind": ekind,
                        "id": eid,
                        "user_id": str(srow["user_id"]),
                        "before": {"model": cur_fields.get("model")},
                        "after": {"model": target_model},
                        "before_updated_at": _ts_str(srow.get("updated_at")),
                        "after_updated_at": _ts_str(upd["updated_at"]),
                    }
                    restored_tasks.append(item)
                    rollback_side_effects.append(item)

            # STEP 3: Post-restore 6-source reference check for dropped models
            dropped_keys = set((cur_compiled.model_config.llm_config or {}).keys()) - target_active_models
            if dropped_keys:
                try:
                    post_refs = await collect_classified_model_references(dropped_keys, conn=conn)
                except ReferenceSourceUnavailable as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "reference_source_unavailable",
                            "source": exc.source,
                            "message": f"Reference check failed ({exc.source}); rollback refused.",
                        },
                    ) from exc

                for dkey in sorted(dropped_keys):
                    # Filter post_refs for this dropped key
                    d_refs = {
                        "agent_config": {k: v for k, v in post_refs["agent_config"].items() if k == dkey},
                        "dalton_settings": {k: v for k, v in post_refs["dalton_settings"].items() if k == dkey},
                        "bundled_plugin_subagents": {
                            k: v for k, v in post_refs["bundled_plugin_subagents"].items() if k == dkey
                        },
                        "plugin_subagents": [r for r in post_refs["plugin_subagents"] if r["model"] == dkey],
                        "user_subagents": [r for r in post_refs["user_subagents"] if r["model"] == dkey],
                        "automations": [r for r in post_refs["automations"] if r["llm_model"] == dkey],
                        "user_preferences": [],
                    }
                    has_blocking = any(
                        bool(d_refs[k])
                        for k in (
                            "agent_config",
                            "dalton_settings",
                            "bundled_plugin_subagents",
                            "plugin_subagents",
                            "user_subagents",
                            "automations",
                        )
                    )
                    if has_blocking:
                        succ = data.successor_for_dropped.get(dkey)
                        try:
                            extra_se, extra_pref_users = await _validate_and_replace_references_in_tx(
                                conn,
                                retired_key=dkey,
                                successor_model=succ,
                                refs=d_refs,
                                post_compiled=target_compiled,
                                actor_user_id=actor_user_id,
                            )
                        except CatalogConflictError as exc:
                            if exc.code == "model_in_use":
                                exc.extra["dropped_model"] = dkey
                                exc.extra["target_revision_models"] = [
                                    {
                                        "model_key": str(mk),
                                        "display_name": str((me or {}).get("display_name") or mk),
                                        "provider": str((me or {}).get("provider") or ""),
                                    }
                                    for mk, me in sorted(
                                        (target_compiled.model_config.llm_config or {}).items()
                                    )
                                    if isinstance(me, Mapping)
                                ]
                            raise
                        rollback_side_effects.extend(extra_se)
                        affected_pref_users.update(extra_pref_users)

            # STEP 4: Commit revision C + 1 with restored_from = T
            saved = await save_catalog_revision(
                conn,
                expected_revision=cur_rev,
                action="rollback",
                target_keys=sorted(dropped_keys),
                payload=target_payload,
                diff_summary={
                    "restored_from": data.target_revision,
                    "dropped_models": sorted(dropped_keys),
                    "restored_tasks_count": len(restored_tasks),
                    "skipped_user_modified_count": len(skipped_user_modified),
                },
                side_effects=rollback_side_effects,
                actor_user_id=actor_user_id,
                comment=data.comment or f"Rollback to revision {data.target_revision}",
                restored_from=data.target_revision,
            )

    new_rev = int(saved["catalog"]["revision"])
    cache_compiled_catalog(target_compiled)
    await publish_catalog_changed(new_rev)
    await invalidate_catalog_redis_caches(actor_user_id=actor_user_id, compiled=target_compiled)
    if affected_pref_users:
        from src.server.database.user import invalidate_user_prefs_cache

        for uid in affected_pref_users:
            try:
                await invalidate_user_prefs_cache(uid)
            except Exception:
                pass

    return {
        "revision": new_rev,
        "restored_from": data.target_revision,
        "restored_tasks": restored_tasks,
        "skipped_user_modified_tasks": skipped_user_modified,
        "revision_record": saved["revision_record"],
    }


# ---------------------------------------------------------------------------
# Minimal admin test-call endpoint (§3.3)
# ---------------------------------------------------------------------------


async def run_catalog_test_call(
    *,
    actor_user_id: str,
    data: CatalogTestCallInput,
) -> dict[str, Any]:
    """Run a 15s-bounded, 32-token ``dalton_probe_ping`` diagnostic call (§3.3)."""
    if not await throttle(f"catalog_test_call:{actor_user_id}", 3, 10):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Too many test-call requests (max 3 per 10 seconds).",
            },
            headers={"Retry-After": "10"},
        )

    compiled = await get_compiled_catalog_async()
    mc = compiled.model_config
    provider = data.provider.strip()
    prov_info = mc.get_provider_info(provider)
    is_catalog_variant = bool(prov_info.get("catalog_defined")) or bool(
        data.base_url and not prov_info
    )

    from src.dalton.database.team_model_catalog import get_catalog_provider_credential
    from src.dalton.llm.tool_probe import _OPENAI_CHAT_TOOL, _PROBE_TOOL_NAME
    from src.dalton.services.team_model_catalog.constants import catalog_variant_source_tag
    from src.llms.llm import LLM

    try:
        if is_catalog_variant:
            target_url = str(data.base_url or prov_info.get("base_url") or "").strip()
            await preflight_catalog_variant_url(target_url)
            target_canonical = canonical_url_key(target_url)
            use_api_key = str(data.api_key or "").strip()
            verify_db = False
            expected_fp: str | None = None
            if not use_api_key:
                stored = await get_catalog_provider_credential(provider, decrypt=True)
                if (
                    stored is None
                    or not stored.get("api_key")
                    or stored.get("canonical_bound_url") != target_canonical
                ):
                    raise CatalogValidationError(
                        "variant_credential_required",
                        f"No stored credential bound to {target_url!r} for variant {provider!r}; supply api_key.",
                    )
                use_api_key = str(stored["api_key"])
                expected_fp = str(stored["key_fingerprint"])
                verify_db = True

            transport_factory = make_catalog_variant_transport_factory(
                provider_slug=provider,
                bound_base_url=target_url,
                expected_key_fingerprint=expected_fp,
                verify_db_binding=verify_db,
            )
            sdk_name = str(data.sdk or prov_info.get("sdk") or "openai")
            custom_cfg = {
                "name": data.model_key or data.model_id,
                "model_id": data.model_id,
                "provider": provider if prov_info else sdk_name,
                "parameters": {**(data.parameters or {}), "max_tokens": 32},
                "extra_body": data.extra_body or {},
            }
            llm = LLM.from_custom_config(
                custom_cfg,
                api_key=use_api_key,
                base_url_override=target_url,
                egress_transport=transport_factory,
                follow_redirects=False,
                _catalog_credential_source=catalog_variant_source_tag(provider),
                _allow_catalog_variant_test_call=True,
            )
        else:
            # Built-in provider: use team credential holder's key (or server env key)
            # and fixed factory default or holder's saved base_url.
            from src.dalton.credentials import credential_user_id
            from src.server.database.api_keys import get_byok_config_for_provider

            holder_id = credential_user_id(actor_user_id)
            byok_cfg = await get_byok_config_for_provider(holder_id, provider)
            use_api_key = (byok_cfg or {}).get("api_key")
            use_base_url = (byok_cfg or {}).get("base_url") or prov_info.get("base_url")
            custom_cfg = {
                "name": data.model_key or data.model_id,
                "model_id": data.model_id,
                "provider": provider,
                "parameters": {**(data.parameters or {}), "max_tokens": 32},
                "extra_body": data.extra_body or {},
            }
            llm = LLM.from_custom_config(
                custom_cfg,
                api_key=use_api_key,
                base_url_override=use_base_url,
                follow_redirects=False,
            )

        bound_llm = llm.bind_tools([_OPENAI_CHAT_TOOL])
        resp = await asyncio.wait_for(
            bound_llm.ainvoke(
                [
                    (
                        "user",
                        f"Call the `{_PROBE_TOOL_NAME}` tool once with value='ok'.",
                    )
                ]
            ),
            timeout=15.0,
        )
        tool_calls = getattr(resp, "tool_calls", None) or []
        return {
            "ok": True,
            "tool_calling_verified": any(
                isinstance(tc, Mapping) and tc.get("name") == _PROBE_TOOL_NAME
                for tc in tool_calls
            ),
            "provider": provider,
            "model_id": data.model_id,
        }
    except (CatalogValidationError, CatalogConflictError, HTTPException):
        raise
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": "timeout",
            "message": "Diagnostic test call timed out after 15s.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": sanitize_error_text(exc),
        }
