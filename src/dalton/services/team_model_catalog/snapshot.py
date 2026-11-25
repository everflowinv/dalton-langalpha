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
from dataclasses import dataclass
from typing import Any

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.billing import resolve_model_billing
from src.dalton.services.team_model_catalog.constants import (
    TURN_SNAPSHOT_SCHEMA,
    norm_id,
    normalize_cash_pricing_card,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotClientParams:
    """Client construction parameters derived from a snapshotted model entry."""

    model_key: str
    provider: str
    model_id: str
    parameters: dict[str, Any]
    extra_body: dict[str, Any]
    additional_betas: list[str]
    reasoning: dict[str, Any] | None
    use_response_api: bool | None
    system_provider: str | None
    provider_info: dict[str, Any]
    catalog_defined: bool
    bound_base_url: str | None
    canonical_bound_url: str | None
    key_fingerprint: str | None


@dataclass(frozen=True)
class RunRecoveryContext:
    """Authoritative recovery context loaded for a persisted main or subagent run."""

    run_id: str
    is_subagent: bool
    model_key: str | None
    snapshot: dict[str, Any] | None
    client_params: SnapshotClientParams | None


_ACTIVE_CATALOG_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar(
    "dalton_active_catalog_snapshot", default=None
)
_ACTIVE_RECOVERY_CONTEXT: ContextVar[RunRecoveryContext | None] = ContextVar(
    "dalton_active_recovery_context", default=None
)


def get_active_catalog_snapshot() -> dict[str, Any] | None:
    """Return the ``effective_catalog_snapshot`` bound to the current async/thread context."""
    return _ACTIVE_CATALOG_SNAPSHOT.get()


def get_active_recovery_context() -> RunRecoveryContext | None:
    """Return the active :class:`RunRecoveryContext` bound to the current execution context."""
    return _ACTIVE_RECOVERY_CONTEXT.get()


@contextmanager
def bind_catalog_snapshot(snapshot: dict[str, Any] | None) -> Iterator[dict[str, Any] | None]:
    """Bind ``snapshot`` to the current execution context for the duration of the block."""
    token = _ACTIVE_CATALOG_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _ACTIVE_CATALOG_SNAPSHOT.reset(token)


@contextmanager
def bind_recovery_context(
    recovery_ctx: RunRecoveryContext | None,
) -> Iterator[RunRecoveryContext | None]:
    """Bind ``recovery_ctx`` and its ``snapshot`` to the current execution context."""
    if recovery_ctx is None:
        yield None
        return
    ctx_token = _ACTIVE_RECOVERY_CONTEXT.set(recovery_ctx)
    snap_token = _ACTIVE_CATALOG_SNAPSHOT.set(recovery_ctx.snapshot)
    try:
        yield recovery_ctx
    finally:
        _ACTIVE_CATALOG_SNAPSHOT.reset(snap_token)
        _ACTIVE_RECOVERY_CONTEXT.reset(ctx_token)


def _is_oauth_blocked_by_capability(
    model_key: str | None = None,
    provider: str | None = None,
) -> bool:
    """Return True when ``NATIVE_OAUTH_SYNC`` is disabled and the target is an OAuth model/provider."""
    from src.dalton.capabilities import CapabilityId, has_capability
    from src.dalton.llm.overlay import _is_oauth_model_key

    if has_capability(CapabilityId.NATIVE_OAUTH_SYNC):
        return False
    mk = str(model_key or "").strip().lower()
    prov = str(provider or "").strip().lower()
    return (bool(mk) and _is_oauth_model_key(mk)) or (bool(prov) and _is_oauth_model_key(prov))


def enforce_snapshot_capabilities(
    model_key: str | None,
    snapshot: Mapping[str, Any] | None,
) -> None:
    """Refuse resuming a snapshotted model whose required deployment capability is currently off.

    Raises the exact same ``HTTPException(400, type="model_removed")`` as a normal request
    for a model stripped by capability gating.
    """
    from src.server.services.llm.availability import raise_model_removed

    snap_provider: str | None = None
    if isinstance(snapshot, Mapping) and model_key:
        models = snapshot.get("models")
        if isinstance(models, Mapping):
            raw = models.get(model_key)
            if isinstance(raw, Mapping):
                snap_provider = str(raw.get("provider") or "").strip() or None
    if _is_oauth_blocked_by_capability(model_key=model_key, provider=snap_provider):
        raise_model_removed(str(model_key or snap_provider or "oauth"), [])


def _output_limit_param_key(
    params: Mapping[str, Any],
    *,
    live_params: Mapping[str, Any] | None = None,
    provider_info: Mapping[str, Any] | None = None,
) -> str:
    """Choose the model's output-limit request parameter key (``max_output_tokens`` vs ``max_tokens``)."""
    if "max_output_tokens" in params:
        return "max_output_tokens"
    if isinstance(live_params, Mapping) and "max_output_tokens" in live_params:
        return "max_output_tokens"
    sdk = str((provider_info or {}).get("sdk") or "").strip().lower()
    if sdk == "gemini":
        return "max_output_tokens"
    return "max_tokens"


def _normalize_snapshot_parameters(
    raw: Mapping[str, Any],
    *,
    live_entry: Mapping[str, Any] | None = None,
    same_live_route: bool = False,
    provider_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build request parameters from a snapshot entry, translating legacy ``max_output`` into the output-limit field."""
    live_params = (
        live_entry.get("parameters")
        if isinstance(live_entry, Mapping) and isinstance(live_entry.get("parameters"), Mapping)
        else None
    )
    if "parameters" in raw and isinstance(raw.get("parameters"), Mapping):
        params = copy.deepcopy(dict(raw["parameters"]))
    elif same_live_route and live_params is not None:
        params = copy.deepcopy(dict(live_params))
    else:
        params = {}

    raw_max_output = raw.get("max_output")
    has_legacy_max_output = (
        isinstance(raw_max_output, int)
        and not isinstance(raw_max_output, bool)
        and raw_max_output > 0
    )
    if has_legacy_max_output and (
        "parameters" not in raw
        or ("max_tokens" not in params and "max_output_tokens" not in params)
    ):
        eff_pinfo = (
            provider_info
            if isinstance(provider_info, Mapping) and provider_info
            else (raw.get("provider_info") if isinstance(raw.get("provider_info"), Mapping) else None)
        )
        out_key = _output_limit_param_key(
            params,
            live_params=live_params,
            provider_info=eff_pinfo,
        )
        params[out_key] = raw_max_output
    return params


def extract_snapshot_client_params(
    model_key: str,
    snapshot: Mapping[str, Any] | None,
    *,
    base_model_config: Any = None,
    enforce_capabilities: bool = True,
) -> SnapshotClientParams | None:
    """Derive normalized client construction parameters for ``model_key`` from ``snapshot``."""
    mk = str(model_key or "").strip()
    if not mk or not isinstance(snapshot, Mapping):
        return None
    models = snapshot.get("models")
    if not isinstance(models, Mapping):
        return None
    raw = models.get(mk)
    if not isinstance(raw, Mapping):
        return None

    if enforce_capabilities:
        enforce_snapshot_capabilities(mk, snapshot)

    snap_provider = str(raw.get("provider") or "").strip()
    snap_model_id = str(raw.get("model_id") or mk).strip()
    if not enforce_capabilities and _is_oauth_blocked_by_capability(
        model_key=mk, provider=snap_provider
    ):
        return None

    live_entry: Mapping[str, Any] | None = None
    live_pinfo: Mapping[str, Any] = {}
    if base_model_config is not None:
        live_entry = base_model_config.get_model_config(mk)
        if snap_provider:
            live_pinfo = base_model_config.get_provider_info(snap_provider) or {}

    same_live_route = (
        isinstance(live_entry, Mapping)
        and str(live_entry.get("provider") or "").strip() == snap_provider
    )
    pinfo = copy.deepcopy(dict(live_pinfo)) if isinstance(live_pinfo, Mapping) else {}
    variants = snapshot.get("variants")
    v_entry = variants.get(snap_provider) if isinstance(variants, Mapping) else None
    if isinstance(raw.get("provider_info"), Mapping):
        pinfo.update(copy.deepcopy(dict(raw["provider_info"])))
    if isinstance(v_entry, Mapping) and isinstance(v_entry.get("provider_info"), Mapping):
        pinfo.update(copy.deepcopy(dict(v_entry["provider_info"])))

    is_catalog_variant = bool(raw.get("catalog_defined") or pinfo.get("catalog_defined"))
    bound_base_url = (
        str(raw.get("bound_base_url")).strip()
        if raw.get("bound_base_url")
        else (
            str(v_entry.get("bound_base_url")).strip()
            if isinstance(v_entry, Mapping) and v_entry.get("bound_base_url")
            else (str(pinfo.get("base_url")).strip() if pinfo.get("base_url") else None)
        )
    )
    canonical_bound_url = (
        str(raw.get("canonical_bound_url")).strip()
        if raw.get("canonical_bound_url")
        else (
            str(v_entry.get("canonical_bound_url")).strip()
            if isinstance(v_entry, Mapping) and v_entry.get("canonical_bound_url")
            else (canonical_url_key(bound_base_url) if bound_base_url else None)
        )
    )
    key_fingerprint = (
        str(raw.get("key_fingerprint")).strip()
        if raw.get("key_fingerprint")
        else (
            str(v_entry.get("key_fingerprint")).strip()
            if isinstance(v_entry, Mapping) and v_entry.get("key_fingerprint")
            else None
        )
    )
    if is_catalog_variant:
        pinfo["catalog_defined"] = True
        pinfo["own_api_key"] = True
        pinfo.pop("env_key", None)
        pinfo.pop("models_url", None)
        pinfo.pop("platform", None)
        if bound_base_url:
            pinfo["base_url"] = bound_base_url

    params = _normalize_snapshot_parameters(
        raw,
        live_entry=live_entry,
        same_live_route=same_live_route,
        provider_info=pinfo,
    )
    if "extra_body" in raw and isinstance(raw.get("extra_body"), Mapping):
        extra_body = copy.deepcopy(dict(raw["extra_body"]))
    elif same_live_route and isinstance(live_entry, Mapping) and isinstance(live_entry.get("extra_body"), Mapping):
        extra_body = copy.deepcopy(dict(live_entry["extra_body"]))
    else:
        extra_body = {}
    if "additional_betas" in raw and isinstance(raw.get("additional_betas"), list):
        additional_betas = [str(b) for b in raw["additional_betas"]]
    elif same_live_route and isinstance(live_entry, Mapping) and isinstance(live_entry.get("additional_betas"), list):
        additional_betas = [str(b) for b in live_entry["additional_betas"]]
    else:
        additional_betas = []
    reasoning = (
        copy.deepcopy(dict(raw["reasoning"]))
        if isinstance(raw.get("reasoning"), Mapping)
        else (
            copy.deepcopy(dict(live_entry["reasoning"]))
            if same_live_route
            and "reasoning" not in raw
            and isinstance(live_entry, Mapping)
            and isinstance(live_entry.get("reasoning"), Mapping)
            else None
        )
    )
    use_response_api = (
        raw.get("use_response_api")
        if "use_response_api" in raw
        else (live_entry.get("use_response_api") if same_live_route and isinstance(live_entry, Mapping) else None)
    )
    system_provider = (
        raw.get("system_provider")
        if "system_provider" in raw
        else (live_entry.get("system_provider") if same_live_route and isinstance(live_entry, Mapping) else None)
    )

    return SnapshotClientParams(
        model_key=mk,
        provider=snap_provider,
        model_id=snap_model_id,
        parameters=params,
        extra_body=extra_body,
        additional_betas=additional_betas,
        reasoning=reasoning,
        use_response_api=use_response_api,
        system_provider=system_provider,
        provider_info=pinfo,
        catalog_defined=is_catalog_variant,
        bound_base_url=bound_base_url,
        canonical_bound_url=canonical_bound_url,
        key_fingerprint=key_fingerprint,
    )


def _extract_model_key_value(val: Any) -> str | None:
    """Extract a model key string from either a scalar key or a ``SubagentModel`` dict ``{"id": ...}``."""
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, Mapping):
        inner = val.get("id")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return None


def _extract_primary_model_key_from_run(
    run_row: Mapping[str, Any],
    *,
    is_subagent: bool,
    snapshot: Mapping[str, Any] | None = None,
) -> str | None:
    """Extract the actual selected model key of the original persisted run."""
    container_key = "replay_facts" if is_subagent else "metadata"
    container = run_row.get(container_key)
    field_order = (
        ("model_override", "model", "llm_model")
        if is_subagent
        else ("llm_model", "main_model", "model", "model_override")
    )
    if isinstance(container, Mapping):
        for fname in field_order:
            extracted = _extract_model_key_value(container.get(fname))
            if extracted:
                return extracted
    for fname in field_order:
        extracted = _extract_model_key_value(run_row.get(fname))
        if extracted:
            return extracted
    if isinstance(snapshot, Mapping):
        models = snapshot.get("models")
        if isinstance(models, Mapping) and len(models) == 1:
            only_key = next(iter(models.keys()))
            if isinstance(only_key, str) and only_key.strip():
                return only_key.strip()
    return None


async def _fetch_persisted_run_row(
    run_id: str,
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
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
    return dict(row) if row else None


async def load_run_recovery_context(
    run_id: str | None = None,
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    run_row: Mapping[str, Any] | None = None,
    explicit_model_key: str | None = None,
    conn: Any = None,
    enforce_capabilities: bool = True,
) -> RunRecoveryContext | None:
    """Single authoritative recovery entry point for main turns and subagents.

    Given an original run identifier (and optional already-loaded ``run_row``), loads or
    backfills its ``effective_catalog_snapshot``, resolves the original run's actual
    selected ``model_key``, enforces current deployment capabilities (e.g. rejecting
    ``*-oauth`` models when ``NATIVE_OAUTH_SYNC`` is disabled with the exact same error
    as a normal request), and derives normalized :class:`SnapshotClientParams` (including
    legacy ``max_output`` translation).
    """
    row: dict[str, Any] | None
    if isinstance(run_row, Mapping):
        row = dict(run_row)
    elif run_id:
        row = await _fetch_persisted_run_row(
            str(run_id), thread_id=thread_id, is_subagent=is_subagent, conn=conn
        )
    else:
        row = None
    if not row:
        return None

    resolved_run_id = str(
        run_id
        or row.get("task_run_id" if is_subagent else "conversation_response_id")
        or row.get("run_id")
        or ""
    ).strip()

    explicit_mk = str(explicit_model_key or "").strip() or None
    snapshot = await ensure_run_catalog_snapshot(
        row,
        model_keys=([explicit_mk] if explicit_mk else None),
        is_subagent=is_subagent,
        conn=conn,
    )
    orig_model_key = _extract_primary_model_key_from_run(
        row, is_subagent=is_subagent, snapshot=snapshot
    )
    effective_model_key = explicit_mk or orig_model_key

    # Only apply the predecessor snapshot when staying on a model covered by it.
    # If the caller explicitly switched to a different model outside the snapshot,
    # do not force the old snapshot onto the new model.
    snap_models = snapshot.get("models") if isinstance(snapshot, Mapping) else None
    effective_snapshot: dict[str, Any] | None = snapshot
    if (
        explicit_mk is not None
        and orig_model_key is not None
        and explicit_mk != orig_model_key
        and (not isinstance(snap_models, Mapping) or explicit_mk not in snap_models)
    ):
        effective_snapshot = None

    if enforce_capabilities and effective_model_key:
        enforce_snapshot_capabilities(effective_model_key, effective_snapshot)

    base_mc = None
    try:
        from src.llms.llm import LLM

        live_mc = LLM.get_model_config()
        base_mc = live_mc._base if isinstance(live_mc, SnapshotModelConfig) else live_mc
    except Exception:
        base_mc = None

    client_params = (
        extract_snapshot_client_params(
            effective_model_key,
            effective_snapshot,
            base_model_config=base_mc,
            enforce_capabilities=enforce_capabilities,
        )
        if effective_model_key and effective_snapshot is not None
        else None
    )

    return RunRecoveryContext(
        run_id=resolved_run_id,
        is_subagent=is_subagent,
        model_key=effective_model_key,
        snapshot=effective_snapshot,
        client_params=client_params,
    )


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
    ctx = await load_run_recovery_context(
        run_id,
        thread_id=thread_id,
        is_subagent=is_subagent,
        conn=conn,
        enforce_capabilities=False,
    )
    return ctx.snapshot if ctx is not None else None


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
    ambiguous_identities: set[str] = set()
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
        billing = resolve_model_billing(
            model_key=key,
            provider=provider,
            model_id=model_id,
            model_config=mc,
            provider_info=prov_info,
            dalton_meta=meta_map,
            dalton_meta_entry=meta_entry,
        )
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
            id_key = _identity_key(provider, model_id)
            id_record = {
                "model_key": key,
                "provider": provider,
                "model_id": model_id,
                "billing": billing,
                "pricing": cash_card,
                "notional_card": notional_card,
            }
            if id_key in ambiguous_identities:
                pass
            elif id_key in by_identity and by_identity[id_key].get("billing") != billing:
                # Two entries with conflicting billing on the same (provider, model_id): drop
                # from unkeyed by_identity so callers must match their own model_key in models.
                by_identity.pop(id_key, None)
                ambiguous_identities.add(id_key)
            else:
                by_identity[id_key] = id_record

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
    want_id = model_id or model_name
    models = snapshot.get("models")
    # 1) Always match the caller's own model_key first (so legacy mixed-billing snapshots never
    # let a sibling entry on the same (provider, model_id) override the caller's own key).
    if isinstance(models, Mapping):
        if model_name and isinstance(models.get(model_name), Mapping):
            cand = models[model_name]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
        if model_id and isinstance(models.get(model_id), Mapping):
            cand = models[model_id]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
    # 2) Unkeyed / alias fallback via by_identity, then unambiguous route scan in models.
    by_identity = snapshot.get("by_identity")
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
    if isinstance(models, Mapping) and provider and want_id:
        matches = [
            item
            for item in models.values()
            if isinstance(item, Mapping)
            and _entry_matches_request(item, provider=provider, model_id=want_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if matches and len({str(m.get("billing") or "") for m in matches}) == 1:
            return matches[0]
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
        if _is_oauth_blocked_by_capability(model_key=model_name, provider=snap_provider):
            return None
        live_entry = self._base.get_model_config(model_name)
        same_live_route = (
            isinstance(live_entry, Mapping)
            and str(live_entry.get("provider") or "").strip() == snap_provider
        )
        out: dict[str, Any] = copy.deepcopy(dict(live_entry)) if same_live_route else {}
        out["provider"] = snap_provider
        out["model_id"] = snap_model_id
        snap_pinfo = self._snap_provider_overlay(snap_provider) or (
            self._base.get_provider_info(snap_provider) if snap_provider else {}
        )
        out["parameters"] = _normalize_snapshot_parameters(
            raw,
            live_entry=live_entry,
            same_live_route=same_live_route,
            provider_info=snap_pinfo,
        )
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
        if not prov or _is_oauth_blocked_by_capability(provider=prov):
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
            extracted = _extract_model_key_value(container.get(field_name))
            if extracted:
                keys.append(extracted)
        fallbacks = container.get("fallback_models")
        if isinstance(fallbacks, list):
            for fb in fallbacks:
                if isinstance(fb, str) and fb.strip():
                    keys.append(fb.strip())
    for field_name in ("model", "llm_model"):
        extracted = _extract_model_key_value(run_row.get(field_name))
        if extracted:
            keys.append(extracted)
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
