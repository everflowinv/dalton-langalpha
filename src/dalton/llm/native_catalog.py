"""Native-subscription model listing, catalog diffing, and runtime overlay writing.

Fetches each native provider's available-model list with the team credential's
token, diffs it against the merged catalog, and builds add-only entries for the
runtime overlay at `var/dalton/catalog/native_sync_overlay.json`. Never edits
anything under `src/` and never adds a model to a default setting. The only
removal path is the PM-driven `retired` list in the same runtime file: the
overlay merge hides those keys, and a restore unhides them — model entries
themselves are never deleted. Tokens reach the fetchers as call arguments only;
they never appear in listings, diffs, errors, or the overlay.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import functools
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.llm.openclaw_catalog import (
    _REASONING_LEVEL_MAP,
    _normalize_reasoning_efforts,
    REPO_ROOT,
    assert_no_credentials,
    infer_family_from_model_id,
)

logger = logging.getLogger(__name__)

NATIVE_SYNC_OVERLAY_PATH = REPO_ROOT / "var" / "dalton" / "catalog" / "native_sync_overlay.json"
NATIVE_SYNC_OVERLAY_SCHEMA = "dalton-native-sync-overlay/1"

FETCH_TIMEOUT_S = 15.0

# The codex backend filters newer models out of /models unless client_version
# >= 0.155.0 (wire-verified by lead); pin a recent codex_cli_rs release so the
# full list comes back, and send the same version in the User-Agent.
CODEX_LIST_CLIENT_VERSION = "0.164.0"

# Display names for the settings card; order fixes the card's row order.
NATIVE_LISTING_PROVIDERS: dict[str, str] = {
    "claude-oauth": "Claude 订阅",
    "codex-oauth": "ChatGPT/Codex 订阅",
    "antigravity-oauth": "Google Antigravity",
    "xai-oauth": "xAI SuperGrok",
    "muse-oauth": "Meta Muse",
    "z-ai-cn-coding": "智谱 GLM Coding Plan",
}

# Neither gateway publishes a model-list endpoint we can call with the team
# credential; the card reports these honestly instead of guessing.
UNSUPPORTED_PROVIDERS: dict[str, str] = {
    "muse-oauth": "该供应商不提供模型列表接口",
    "z-ai-cn-coding": "该供应商不提供模型列表接口",
}


@dataclass
class UpstreamModel:
    id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output: int | None = None
    supports_reasoning_effort: bool | None = None
    reasoning_efforts: list[str] | None = None
    reasoning_default: str | None = None
    visibility: str | None = None
    # Upstream-internal entry (antigravity's tab_*/chat_*/*-tiered rows carry no
    # displayName): it still proves the id exists upstream — so a catalog entry
    # with the same id is NOT deprecated — but it is never offered as an addition.
    internal: bool = False


@dataclass
class ProviderListing:
    provider: str
    display_name: str
    status: str  # "ok" | "unsupported" | "error"
    models: list[UpstreamModel] = field(default_factory=list)
    message: str | None = None
    error_kind: str | None = None


# ---------------------------------------------------------------------------
# Per-provider fetchers (each takes an already-resolved token; unit tests drive
# them through httpx.MockTransport, never a real network)
# ---------------------------------------------------------------------------


async def fetch_claude_models(
    access_token: str,
    *,
    base_url: str = "https://api.anthropic.com",
    default_headers: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[UpstreamModel]:
    """GET `/v1/models` on the Anthropic API with the claude-oauth beta headers.

    `default_headers` mirrors the `claude-oauth` variant's `default_headers` in
    the provider manifest (anthropic-beta / user-agent / x-app); passing them in
    keeps this function decoupled from the manifest. `anthropic-version` is
    mandatory on this route (401/400 without it — same header the BYOK probe
    sends, src/server/app/api_keys.py).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "anthropic-version": "2023-06-01",
        **(default_headers or {}),
    }
    models: list[UpstreamModel] = []
    after_id: str | None = None

    async def _do_fetch(c: httpx.AsyncClient) -> list[UpstreamModel]:
        nonlocal after_id
        for _ in range(5):
            params: dict[str, Any] = {"limit": 1000}
            if after_id:
                params["after_id"] = after_id
            resp = await c.get(
                f"{base_url.rstrip('/')}/v1/models", headers=headers, params=params
            )
            resp.raise_for_status()
            payload = resp.json()
            for row in payload.get("data") or []:
                if isinstance(row, dict) and row.get("id"):
                    models.append(
                        UpstreamModel(
                            id=str(row["id"]),
                            display_name=row.get("display_name"),
                        )
                    )
            after_id = payload.get("last_id") if payload.get("has_more") else None
            if not after_id:
                break
        return models

    if client is not None:
        return await _do_fetch(client)
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as new_client:
        return await _do_fetch(new_client)


async def fetch_codex_models(
    access_token: str,
    *,
    account_id: str | None = None,
    base_url: str = "https://chatgpt.com/backend-api/codex",
    client: httpx.AsyncClient | None = None,
) -> list[UpstreamModel]:
    """GET `{base}/models` on the ChatGPT codex backend (`{"models":[{"slug":…}]}`)."""
    # The backend gates newer models on first-party client headers and on
    # client_version >= 0.155.0; both mirror what `_get_codex_llm` sends
    # (src/llms/llm.py), with the version pinned to CODEX_LIST_CLIENT_VERSION.
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "originator": "codex_cli_rs",
        "User-Agent": f"codex_cli_rs/{CODEX_LIST_CLIENT_VERSION}",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    async def _do_fetch(c: httpx.AsyncClient) -> list[UpstreamModel]:
        resp = await c.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            params={"client_version": CODEX_LIST_CLIENT_VERSION},
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = payload.get("data") if isinstance(payload, dict) else None
        out: list[UpstreamModel] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = row.get("slug") or row.get("id")
            if not mid:
                continue
            out.append(
                UpstreamModel(
                    id=str(mid),
                    display_name=row.get("display_name") or row.get("label") or row.get("name"),
                    context_window=(
                        int(row["context_window"])
                        if isinstance(row.get("context_window"), int)
                        else None
                    ),
                    # Codex 0.164.0 protocol (openai_models.rs): each level is an
                    # object {effort, description}; the effort string is the id.
                    reasoning_efforts=(
                        [
                            str(lv["effort"])
                            for lv in row["supported_reasoning_levels"]
                            if isinstance(lv, dict) and lv.get("effort")
                        ]
                        if isinstance(row.get("supported_reasoning_levels"), list)
                        and row.get("supported_reasoning_levels")
                        else None
                    ),
                    reasoning_default=(
                        str(row["default_reasoning_level"])
                        if row.get("default_reasoning_level")
                        else None
                    ),
                    visibility=str(row["visibility"]) if row.get("visibility") else None,
                )
            )
        return out

    if client is not None:
        return await _do_fetch(client)
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as new_client:
        return await _do_fetch(new_client)


async def fetch_antigravity_models(
    access_token: str,
    *,
    base_url: str = "https://daily-cloudcode-pa.googleapis.com",
    user_agent: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[UpstreamModel]:
    """POST `{base}/v1internal:fetchAvailableModels` (empty body) on the Cloud Code endpoint.

    The response's `models` is a dict keyed by the full model id (effort tier
    included, e.g. `gemini-4-argon-high`); every id comes back, but internal
    entries (`tab_*`, `chat_*`, `*-tiered`) carry no `displayName` and are
    marked `internal` — they match for deprecation checks yet are never
    offered as additions.
    """
    from src.dalton.llm.providers.antigravity.client import ANTIGRAVITY_USER_AGENT

    # Without a User-Agent naming the CLI client the backend answers 403
    # SUBSCRIPTION_REQUIRED — same constraint as the chat endpoint.
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent or ANTIGRAVITY_USER_AGENT,
    }

    async def _do_fetch(c: httpx.AsyncClient) -> list[UpstreamModel]:
        resp = await c.post(
            f"{base_url.rstrip('/')}/v1internal:fetchAvailableModels",
            json={},
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, dict):
            return []
        out: list[UpstreamModel] = []
        # Every id in the dict participates in the deprecation check; only the
        # ones with a displayName are user-facing additions.
        for model_id, row in rows.items():
            if not model_id or not isinstance(row, dict):
                continue
            internal = not row.get("displayName")
            out.append(
                UpstreamModel(
                    id=str(model_id),
                    display_name=None if internal else str(row["displayName"]),
                    context_window=(
                        int(row["maxTokens"]) if isinstance(row.get("maxTokens"), int) else None
                    ),
                    max_output=(
                        int(row["maxOutputTokens"])
                        if isinstance(row.get("maxOutputTokens"), int)
                        else None
                    ),
                    internal=internal,
                )
            )
        return out

    if client is not None:
        return await _do_fetch(client)
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as new_client:
        return await _do_fetch(new_client)


# ---------------------------------------------------------------------------
# Orchestration: resolve the team credential's token per provider, fetch all
# listings concurrently, and isolate every failure to its provider.
# ---------------------------------------------------------------------------


async def _resolve_provider_token(provider: str, user_id: str) -> dict[str, Any] | None:
    """A usable access token for the provider, through the one unified refresh
    protocol (``oauth_health.valid_token``).

    The old per-provider ``get_valid_token`` refreshers were removed with the
    upstream #623 rework; this door refreshes under the shared lock and saves
    conditionally, for every registered subscription line alike.
    """
    from src.server.services.oauth_providers import SUBSCRIPTIONS

    if provider not in SUBSCRIPTIONS:
        return None
    from src.server.services.oauth_health import (
        OAuthReauthRequired,
        OAuthRefreshUnavailable,
        valid_token,
    )

    try:
        token = await valid_token(user_id, provider)
    except (OAuthReauthRequired, OAuthRefreshUnavailable):
        # A connection that cannot serve this check now is, for this check,
        # no credential; the status page says which and why.
        return None
    if token is None or not token.access_token:
        return None
    return {
        "access_token": token.access_token,
        "account_id": token.account_id,
        "plan_type": token.plan_type,
    }


def _provider_fetch_inputs(provider: str, token: dict[str, Any]) -> dict[str, Any]:
    """Endpoint base URLs and header blocks for `provider`, read off the live catalog."""
    from src.llms.llm import LLM

    pinfo = LLM.get_model_config().get_provider_info(provider) or {}
    base_url = str(pinfo.get("base_url") or "")
    out: dict[str, Any] = {"base_url": base_url} if base_url else {}
    if provider == "claude-oauth":
        out["default_headers"] = dict(pinfo.get("default_headers") or {})
    if provider == "codex-oauth" and token.get("account_id"):
        out["account_id"] = str(token["account_id"])
    return out


async def _collect_one_listing(provider: str, user_id: str) -> ProviderListing:
    display = NATIVE_LISTING_PROVIDERS.get(provider, provider)
    unsupported = UNSUPPORTED_PROVIDERS.get(provider)
    if unsupported:
        return ProviderListing(
            provider=provider, display_name=display, status="unsupported", message=unsupported
        )

    try:
        token = await _resolve_provider_token(provider, user_id)
    except Exception as exc:
        logger.warning("[native_catalog] token resolution failed for %s: %s", provider, type(exc).__name__)
        token = None
    if not token or not token.get("access_token"):
        return ProviderListing(
            provider=provider,
            display_name=display,
            status="error",
            error_kind="no_credentials",
            message="该供应商未连接，无法检查",
        )

    access_token = str(token["access_token"])
    try:
        fetch_inputs = _provider_fetch_inputs(provider, token)
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            if provider == "xai-oauth":
                from src.dalton.llm.providers.xai_grok.oauth import fetch_xai_models

                rows = await fetch_xai_models(access_token, **fetch_inputs)
                models = [
                    UpstreamModel(
                        id=str(r.get("id") or ""),
                        display_name=(str(r["name"]) if r.get("name") else None),
                        context_window=r.get("context_window"),
                        supports_reasoning_effort=(
                            bool(r.get("supports_reasoning_effort"))
                            if r.get("supports_reasoning_effort") is not None
                            else None
                        ),
                        reasoning_efforts=(
                            [str(lv) for lv in r["reasoning_efforts"]]
                            if isinstance(r.get("reasoning_efforts"), list)
                            and r.get("reasoning_efforts")
                            else None
                        ),
                    )
                    for r in rows
                    if r.get("id")
                ]
            elif provider == "claude-oauth":
                models = await fetch_claude_models(access_token, **fetch_inputs)
            elif provider == "codex-oauth":
                models = await fetch_codex_models(access_token, **fetch_inputs)
            elif provider == "antigravity-oauth":
                models = await fetch_antigravity_models(access_token, **fetch_inputs)
            else:  # pragma: no cover - guarded by UNSUPPORTED_PROVIDERS
                models = []
        return ProviderListing(
            provider=provider, display_name=display, status="ok", models=models
        )
    except asyncio.TimeoutError:
        return ProviderListing(
            provider=provider,
            display_name=display,
            status="error",
            error_kind="timeout",
            message=f"拉取超时（>{FETCH_TIMEOUT_S:.0f}s）",
        )
    except httpx.HTTPStatusError as exc:
        return ProviderListing(
            provider=provider,
            display_name=display,
            status="error",
            error_kind=f"http_{exc.response.status_code}",
            message=f"供应商返回 HTTP {exc.response.status_code}",
        )
    except Exception as exc:
        return ProviderListing(
            provider=provider,
            display_name=display,
            status="error",
            error_kind=type(exc).__name__,
            message=sanitize_error_text(exc),
        )


async def collect_native_listings(user_id: str) -> dict[str, ProviderListing]:
    """Fetch every native provider's model list concurrently; one failure never blocks the rest."""
    results = await asyncio.gather(
        *(_collect_one_listing(p, user_id) for p in NATIVE_LISTING_PROVIDERS)
    )
    return {listing.provider: listing for listing in results}


# ---------------------------------------------------------------------------
# Diff against the merged catalog
# ---------------------------------------------------------------------------


def _entry_matches_upstream(entry: dict[str, Any], upstream_id: str) -> bool:
    return str(entry.get("model_id") or "") == upstream_id


def suggest_native_model_key(provider: str, upstream_id: str) -> str:
    # Every fetchable native provider names its keys `<upstream id>-oauth`
    # (claude-opus-5-5-oauth, grok-4.7-oauth, gemini-4-argon-low-oauth).
    return f"{upstream_id}-oauth"


def build_native_diff(
    listings: dict[str, ProviderListing],
    llm_config: dict[str, dict[str, Any]],
    retired_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Classify each provider's catalog entries into added / deprecated / unchanged.

    `retired_keys` (the runtime overlay's retire list) marks an upstream
    re-appearance of a previously retired model as `previously_retired` instead
    of offering it as a plain addition — restoring is an explicit PM action.
    """
    retired_keys = retired_keys or set()
    added: list[dict[str, Any]] = []
    deprecated: list[dict[str, Any]] = []
    unchanged = 0

    for provider in NATIVE_LISTING_PROVIDERS:
        listing = listings.get(provider)
        if listing is None or listing.status != "ok":
            continue
        if not listing.models:
            # A 200 with zero parseable rows makes the listing untrustworthy:
            # without provider rows nothing from this provider may be judged
            # deprecated. Downgrade the listing so both the diff and the
            # provider rows report it as an error.
            listing.status = "error"
            listing.error_kind = "empty_listing"
            listing.message = "返回了空列表或无法解析"
            continue
        current = {
            key: entry
            for key, entry in llm_config.items()
            if isinstance(entry, dict) and entry.get("provider") == provider
        }
        matched_keys: set[str] = set()
        for um in listing.models:
            hit = next(
                (k for k, e in current.items() if _entry_matches_upstream(e, um.id)),
                None,
            )
            if hit is not None:
                matched_keys.add(hit)
                unchanged += 1
                continue
            if um.internal:
                # Present upstream but not user-facing; counted as a match
                # above when a catalog entry shares its id, never an addition.
                continue
            if um.visibility == "hide":
                # Hidden upstream-side (gpt-reserve, codex-auto-review); not
                # offered as an addition.
                continue
            suggested = suggest_native_model_key(provider, um.id)
            added.append(
                {
                    "provider": provider,
                    "upstream_id": um.id,
                    "display_name": um.display_name or um.id,
                    "suggested_key": suggested,
                    "conflict": False,
                    "conflict_reason": None,
                    "duplicate_display_name": False,
                    "context": um.context_window,
                    "max_output": um.max_output,
                    "supports_reasoning_effort": um.supports_reasoning_effort,
                    "reasoning_efforts": um.reasoning_efforts,
                    "reasoning_default": um.reasoning_default,
                    "visibility": um.visibility,
                    "previously_retired": suggested in retired_keys,
                }
            )
        for key, entry in sorted(current.items()):
            if key in matched_keys:
                continue
            deprecated.append(
                {
                    "provider": provider,
                    "key": key,
                    "model_id": entry.get("model_id"),
                    "display_name": entry.get("display_name"),
                }
            )

    added.sort(key=lambda a: (a["provider"], a["upstream_id"]))

    # A suggested key that already exists belongs to a different model_id
    # (upstream rename); the PM decides instead of us overwriting.
    for item in added:
        if item["suggested_key"] in llm_config:
            item["conflict"] = True
            item["conflict_reason"] = "key 与现有模型重名（可能是上游改名），需人工确认"

    # Two additions suggesting the same key (across providers or within one)
    # must be surfaced, never silently overwritten.
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in added:
        by_key.setdefault(item["suggested_key"], []).append(item)
    for key, items in by_key.items():
        if len(items) < 2:
            continue
        providers = sorted({i["provider"] for i in items})
        for item in items:
            item["conflict"] = True
            item["conflict_reason"] = f"key 与其他供应商的新增重复：{'、'.join(providers)}"

    # Several upstream ids sometimes share one display name (old aliases); flag
    # them so the PM picks at most one instead of syncing look-alike entries.
    added_name_counts = collections.Counter(i["display_name"] for i in added)
    for item in added:
        same_provider_names = {
            str(e.get("display_name"))
            for e in llm_config.values()
            if isinstance(e, dict)
            and e.get("provider") == item["provider"]
            and e.get("display_name")
        }
        if added_name_counts[item["display_name"]] > 1 or item["display_name"] in same_provider_names:
            item["duplicate_display_name"] = True

    deprecated.sort(key=lambda d: (d["provider"], d["key"]))
    return {"added": added, "deprecated": deprecated, "unchanged": unchanged}


# ---------------------------------------------------------------------------
# Overlay entry construction + atomic add-only write
# ---------------------------------------------------------------------------


@dataclass
class _ProviderProfile:
    """Provider-level (or same-family) protocol facts inherited by new entries.

    Only fields that are uniform across the pool survive; anything that varies
    per model (reasoning efforts, oauth_plans, display_name, per-model max
    tokens) is dropped and filled from the upstream listing instead.
    """

    prompt_guidance: str | None = None
    input_modalities: list[str] | None = None
    uniform_params: dict[str, Any] = field(default_factory=dict)
    max_output_key: str | None = None
    default_context: int | None = None
    reasoning_write: str | None = None
    reasoning_on: dict[str, Any] | None = None
    billing: str = "subscription"
    tool_calling: bool = True


@functools.lru_cache(maxsize=1)
def _known_cash_model_ids() -> frozenset[str]:
    """Bare model ids that carry a cash price card (providers.json + native overlay)."""
    from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_overlay_data
    from src.dalton.llm.openclaw_catalog import load_upstream_bare_pricing

    ids = set(load_upstream_bare_pricing())
    native = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
    for _prov, items in ((native.get("providers") or {}).get("models") or {}).items():
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                ids.add(str(item["id"]))
    return frozenset(ids)


def _provider_profile(
    provider: str,
    family: str,
    llm_config: dict[str, dict[str, Any]],
    dalton_meta: dict[str, dict[str, Any]],
) -> _ProviderProfile | None:
    entries = [
        (key, entry)
        for key, entry in llm_config.items()
        if isinstance(entry, dict) and entry.get("provider") == provider
    ]
    if not entries:
        return None
    # Same-family template first (antigravity mixes Gemini, Claude and GPT-OSS
    # under one provider); fall back to provider-wide uniformity.
    pool = [(k, e) for k, e in entries if (dalton_meta.get(k) or {}).get("family") == family]
    pool = pool or entries

    profile = _ProviderProfile()
    guidance_vals = {e.get("prompt_guidance") for _, e in pool}
    if len(guidance_vals) == 1 and next(iter(guidance_vals)) is not None:
        profile.prompt_guidance = next(iter(guidance_vals))
    modality_vals = [e.get("input_modalities") for _, e in pool]
    if all(m == modality_vals[0] for m in modality_vals) and modality_vals[0]:
        profile.input_modalities = copy.deepcopy(modality_vals[0])

    param_sets = [e.get("parameters") or {} for _, e in pool]
    profile.uniform_params = {
        k: copy.deepcopy(v)
        for k, v in param_sets[0].items()
        if all(p.get(k) == v for p in param_sets[1:])
    }
    for cand in ("max_tokens", "max_output_tokens"):
        if any(cand in p for p in param_sets):
            profile.max_output_key = cand
            break

    context_counter = collections.Counter(
        e.get("context") for _, e in pool if isinstance(e.get("context"), int)
    )
    if context_counter:
        profile.default_context = context_counter.most_common(1)[0][0]

    reasoning_blocks = [e.get("reasoning") or {} for _, e in pool]
    writes = {r.get("write") for r in reasoning_blocks if r.get("write") is not None}
    if len(writes) == 1:
        profile.reasoning_write = next(iter(writes))
        ons = {r.get("on") for r in reasoning_blocks if r.get("on") is not None}
        if len(ons) == 1:
            profile.reasoning_on = copy.deepcopy(next(iter(ons)))

    metas = [dalton_meta.get(k) or {} for k, _ in pool]
    billing_vals = {m.get("billing") for m in metas if m.get("billing")}
    if len(billing_vals) == 1:
        profile.billing = next(iter(billing_vals))
    tool_vals = {m.get("tool_calling") for m in metas if m.get("tool_calling") is not None}
    if len(tool_vals) == 1:
        profile.tool_calling = next(iter(tool_vals))
    return profile


def build_overlay_entries(
    added: list[dict[str, Any]],
    llm_config: dict[str, dict[str, Any]],
    dalton_meta: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build overlay `models` + `dalton_meta` fragments for the selected additions.

    New entries inherit only provider-level protocol facts (see
    `_ProviderProfile`); identity and capability fields come from the upstream
    listing itself, so nothing model-specific (oauth_plans, another family's
    notional_ref, borrowed reasoning efforts) rides along.
    """
    models_out: dict[str, dict[str, Any]] = {}
    meta_out: dict[str, dict[str, Any]] = {}
    for item in added:
        provider = str(item.get("provider") or "")
        key = str(item.get("suggested_key") or "")
        upstream_id = str(item.get("upstream_id") or "")
        if not key or not upstream_id or key in llm_config or key in models_out:
            continue

        family = infer_family_from_model_id(upstream_id)
        profile = _provider_profile(provider, family, llm_config, dalton_meta)
        if profile is None:
            continue

        entry: dict[str, Any] = {
            "model_id": upstream_id,
            "provider": provider,
            "display_name": str(item.get("display_name") or upstream_id),
            "visible": True,
        }
        if profile.prompt_guidance:
            entry["prompt_guidance"] = profile.prompt_guidance
        if profile.input_modalities:
            entry["input_modalities"] = copy.deepcopy(profile.input_modalities)
        context = item.get("context") or profile.default_context
        if context:
            entry["context"] = int(context)

        params = copy.deepcopy(profile.uniform_params)
        if item.get("max_output") and profile.max_output_key:
            params[profile.max_output_key] = int(item["max_output"])
        if params:
            entry["parameters"] = params

        if item.get("supports_reasoning_effort") is not None:
            entry["supports_reasoning_effort"] = bool(item["supports_reasoning_effort"])

        # Efforts only when the upstream listed them — no borrowed defaults.
        raw_efforts = [str(lv) for lv in (item.get("reasoning_efforts") or [])]
        if raw_efforts:
            efforts = _normalize_reasoning_efforts(raw_efforts)
            upstream_default = _REASONING_LEVEL_MAP.get(
                str(item.get("reasoning_default") or "").lower()
            )
            if upstream_default in efforts:
                default_effort = upstream_default
            elif "high" in efforts:
                default_effort = "high"
            else:
                default_effort = efforts[-1]
            reasoning: dict[str, Any] = {
                "efforts": efforts,
                "default": default_effort,
            }
            if profile.reasoning_write:
                reasoning["write"] = profile.reasoning_write
            if profile.reasoning_on:
                reasoning["on"] = copy.deepcopy(profile.reasoning_on)
            entry["reasoning"] = reasoning

        models_out[key] = entry

        meta: dict[str, Any] = {
            "family": family,
            "billing": profile.billing,
            "tool_calling": profile.tool_calling,
        }
        if upstream_id in _known_cash_model_ids():
            meta["notional_ref"] = upstream_id
        meta_out[key] = meta
    return models_out, meta_out


def load_native_sync_overlay(
    path: Path | None = None,
) -> dict[str, Any] | None:
    from src.dalton.llm.overlay import load_overlay_data

    return load_overlay_data(path or NATIVE_SYNC_OVERLAY_PATH)


def load_retired_models(path: Path | None = None) -> list[dict[str, Any]]:
    """Return the runtime overlay's `retired` records (`{key, provider, retired_at, retired_by}`)."""
    retired = (load_native_sync_overlay(path) or {}).get("retired")
    if not isinstance(retired, list):
        return []
    return [dict(item) for item in retired if isinstance(item, dict) and item.get("key")]


def retired_key_set(path: Path | None = None) -> set[str]:
    return {str(item["key"]) for item in load_retired_models(path)}


def read_retired_keys_strict(path: Path | None = None) -> set[str]:
    """The retire list, failing loud for callers that must not mistake a broken
    read for an empty one (confirm-sync's previously-retired gate).

    A missing file is the normal never-retired state and reads as empty; a file
    that cannot be parsed, or is not a JSON object, raises ValueError. The
    tolerant `retired_key_set` sits behind `load_overlay_data`, which swallows
    a corrupt file to None — exactly the silent "nothing retired" this avoids.
    """
    target = path or NATIVE_SYNC_OVERLAY_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception as exc:
        raise ValueError(f"native sync overlay unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("native sync overlay is not a JSON object")
    retired = raw.get("retired")
    if not isinstance(retired, list):
        return set()
    return {
        str(item["key"])
        for item in retired
        if isinstance(item, dict) and item.get("key")
    }


def _write_overlay_atomic(overlay_out: dict[str, Any], target: Path) -> str:
    """Credential-check and atomically replace `target`, returning the new `mtime:size` stamp."""
    assert_no_credentials(overlay_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(overlay_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, target)
    st = target.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _append_history(existing: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    history = list((existing.get("meta") or {}).get("history") or [])
    history.append(event)
    return history[-20:]


def write_native_sync_overlay(
    models: dict[str, dict[str, Any]],
    dalton_meta: dict[str, dict[str, Any]],
    *,
    added_keys: list[str],
    path: Path | None = None,
) -> str:
    """Merge `models`/`dalton_meta` add-only into the runtime overlay, atomically.

    Returns the new `mtime:size` stamp. A key already present in the file is
    skipped untouched — existing entries are never modified or removed, so a
    re-sync of the same key is a no-op. The `retired` list rides along as-is.
    """
    target = path or NATIVE_SYNC_OVERLAY_PATH
    existing = load_native_sync_overlay(target) or {}
    merged_models: dict[str, Any] = dict(existing.get("models") or {})
    merged_meta: dict[str, Any] = dict(existing.get("dalton_meta") or {})
    for key, entry in copy.deepcopy(models).items():
        merged_models.setdefault(key, entry)
    for key, meta in copy.deepcopy(dalton_meta).items():
        merged_meta.setdefault(key, meta)

    overlay_out = {
        "schema": NATIVE_SYNC_OVERLAY_SCHEMA,
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "native-model-sync",
            "model_count": len(merged_models),
            "history": _append_history(
                existing,
                {
                    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "added_keys": sorted(added_keys),
                },
            ),
        },
        "models": merged_models,
        "retired": list(existing.get("retired") or []),
        "providers": {"provider_config": {}, "provider_variants": {}, "models": {}},
        "dalton_meta": merged_meta,
    }
    return _write_overlay_atomic(overlay_out, target)


def retire_native_models(
    records: list[dict[str, Any]],
    *,
    path: Path | None = None,
) -> str:
    """Append retire records (`{key, provider, retired_at, retired_by}`) to the overlay's `retired` list.

    Idempotent per key: a key already retired keeps its original record. Models
    and dalton_meta entries stay in the file untouched, so a restore brings the
    model back exactly as it was.
    """
    target = path or NATIVE_SYNC_OVERLAY_PATH
    existing = load_native_sync_overlay(target) or {}
    retired = [dict(item) for item in existing.get("retired") or [] if isinstance(item, dict)]
    known = {str(item.get("key")) for item in retired}
    added: list[str] = []
    for record in records:
        key = str(record.get("key") or "")
        if not key or key in known:
            continue
        retired.append(dict(record))
        known.add(key)
        added.append(key)
    overlay_out = {
        "schema": NATIVE_SYNC_OVERLAY_SCHEMA,
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "native-model-sync",
            "model_count": len(existing.get("models") or {}),
            # A no-op retire (every key already retired) leaves no history noise.
            "history": (
                _append_history(
                    existing,
                    {
                        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "retired_keys": sorted(added),
                    },
                )
                if added
                else list((existing.get("meta") or {}).get("history") or [])
            ),
        },
        "models": dict(existing.get("models") or {}),
        "retired": retired,
        "providers": dict(existing.get("providers") or {})
        or {"provider_config": {}, "provider_variants": {}, "models": {}},
        "dalton_meta": dict(existing.get("dalton_meta") or {}),
    }
    return _write_overlay_atomic(overlay_out, target)


def restore_native_models(
    keys: list[str],
    *,
    path: Path | None = None,
) -> tuple[str, list[str]]:
    """Remove `keys` from the overlay's `retired` list; return `(stamp, restored_keys)`.

    Only the `retired` record is removed — the model entry itself was never
    deleted, so the merge brings the model straight back into the catalog.
    """
    target = path or NATIVE_SYNC_OVERLAY_PATH
    existing = load_native_sync_overlay(target) or {}
    wanted = {str(k) for k in keys}
    retired: list[dict[str, Any]] = []
    restored: list[str] = []
    for item in existing.get("retired") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("key")) in wanted:
            restored.append(str(item.get("key")))
            continue
        retired.append(dict(item))
    overlay_out = {
        "schema": NATIVE_SYNC_OVERLAY_SCHEMA,
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "native-model-sync",
            "model_count": len(existing.get("models") or {}),
            # A no-op restore (nothing on the retired list) leaves no history noise.
            "history": (
                _append_history(
                    existing,
                    {
                        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "restored_keys": sorted(set(restored)),
                    },
                )
                if restored
                else list((existing.get("meta") or {}).get("history") or [])
            ),
        },
        "models": dict(existing.get("models") or {}),
        "retired": retired,
        "providers": dict(existing.get("providers") or {})
        or {"provider_config": {}, "provider_variants": {}, "models": {}},
        "dalton_meta": dict(existing.get("dalton_meta") or {}),
    }
    return _write_overlay_atomic(overlay_out, target), sorted(set(restored))
