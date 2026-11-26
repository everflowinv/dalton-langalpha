"""The credential a turn would use, the cached list it reads, and the entry
points the routes call. The only layer that reads the database, the OAuth
stores and Redis."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from src.llms.model_spec import wire_api
from src.server.services.oauth_providers import SUBSCRIPTIONS

from .catalog import GatewayCatalog, fetch_section
from .matching import ModelsResult, build_result, filter_others, match_endpoint_models
from .wire import (
    ANTIGRAVITY,  # DALTON-HOOK(llm-sdk-dispatch): guard malformed empty Antigravity payloads
    BAD_RESPONSE,  # DALTON-HOOK(catalog-cache): classify empty Antigravity payloads as bad_response
    CODEX,
    ENDPOINT_REFUSED,
    OPENAI,
    UNAUTHORIZED,
    ListFetchError,
    build_list_request,
    fetch_list,
    list_shape,
    normalize,
)

logger = logging.getLogger(__name__)

CACHE_TTL_S = 6 * 3600
# DALTON-HOOK(catalog-cache): short-term failure cache TTL, stale retention, and per-key write lock
# so transient errors and concurrent refreshes never overwrite a newer successful list.
_FAILURE_CACHE_TTL_S = 60
_STALE_RETENTION_S = 7 * 24 * 3600
_cache_write_locks: dict[tuple[str, str], asyncio.Lock] = {}
# Bumped when the cached shape changes, so an old entry reads as a miss.
_CACHE_SCHEMA = 2
_REFRESH_COOLDOWN_S = 10

# Fallback reasons a subscription gives before any list is read.
NEEDS_REAUTH = "needs_reauth"
REFRESH_UNAVAILABLE = "refresh_unavailable"
NO_API_ACCESS = "no_api_access"  # the subscription's plan has no API access


class UnknownProvider(Exception):
    pass


class UnsupportedProvider(Exception):
    """A provider whose models are not listed here: a local server (its own
    ``/providers/{p}/models`` route) or a platform-only route."""


class NotConnected(Exception):
    """No credential a turn would use for this provider."""


# ---------------------------------------------------------------------------
# The credential a turn would use
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    api_key: str | None = field(repr=False)
    holder: str
    base_url: str | None = None  # saved with the key; None is the manifest's
    account_id: str | None = None
    plan_type: str | None = None
    token_headers: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # DALTON-HOOK(team-model-catalog): key_fingerprint captured from the same DB read as api_key & base_url
    catalog_key_fingerprint: str | None = None

    def fingerprint(self, provider: str, extra: Iterable[str] = ()) -> str:
        if provider in SUBSCRIPTIONS:
            # The access token rotates hourly; the account and plan decide the list.
            parts = [provider, self.account_id or "", self.plan_type or "", *extra]
        else:
            parts = [provider, self.holder, self.api_key or "", self.base_url or "", *extra]
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


class _OAuthUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason


async def _resolve_credential(user_id: str, provider: str, mc) -> Credential:
    """The credential a turn on ``provider`` would use, or ``NotConnected``."""
    subscription = SUBSCRIPTIONS.get(provider)
    if subscription is not None:
        from src.server.services.oauth_health import (
            OAuthPlanNoApiAccess,
            OAuthReauthRequired,
            OAuthRefreshUnavailable,
            valid_token,
        )

        try:
            token = await valid_token(user_id, provider)
        except OAuthReauthRequired as e:
            raise _OAuthUnavailable(NEEDS_REAUTH) from e
        except OAuthRefreshUnavailable as e:
            raise _OAuthUnavailable(REFRESH_UNAVAILABLE) from e
        except OAuthPlanNoApiAccess as e:
            raise _OAuthUnavailable(NO_API_ACCESS) from e
        if not token or not token.access_token:
            raise NotConnected(provider)
        return Credential(
            api_key=token.access_token,
            holder=provider,
            account_id=token.account_id or None,
            plan_type=token.plan_type or None,
            token_headers=subscription.request_headers(token),
        )

    # DALTON-HOOK(team-model-catalog): catalog-defined variants use exclusively their
    # dedicated bound credential in dalton_catalog_provider_credentials (never BYOK or env_key).
    if mc.get_provider_info(provider).get("catalog_defined") is True:
        from src.dalton.database.team_model_catalog import get_catalog_provider_credential

        cat_cred = await get_catalog_provider_credential(provider, decrypt=True)
        if not cat_cred or not cat_cred.get("api_key"):
            raise NotConnected(provider)
        return Credential(
            api_key=str(cat_cred["api_key"]),
            holder=f"catalog_variant:{provider}",
            base_url=str(cat_cred["bound_base_url"]),
            catalog_key_fingerprint=str(cat_cred.get("key_fingerprint") or "") or None,
        )

    from src.server.services.llm.byok import walk_byok_candidates
    from src.server.services.llm.config import deployment_key

    byok_config, holder = await walk_byok_candidates(user_id, provider, mc)
    if byok_config and byok_config.get("api_key"):
        return Credential(
            api_key=byok_config["api_key"], holder=holder, base_url=byok_config.get("base_url") or None,
        )
    server_key = deployment_key(provider)
    if server_key:
        return Credential(api_key=server_key, holder=provider)
    raise NotConnected(provider)


# ---------------------------------------------------------------------------
# Cache: one Redis hash per user, a field per provider holding the fetched
# list and the fingerprint of the credential that fetched it.
# ---------------------------------------------------------------------------


def available_models_key(user_id: str) -> str:
    # DALTON-HOOK(team-credentials): the list is fetched on the credential
    # holder's connection, so the cache is keyed by the holder too — members
    # share one entry, and any member's connect or disconnect clears it.
    from src.dalton.credentials import credential_user_id

    return f"provider_models:{credential_user_id(user_id)}"


def _redis():
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    return cache.client if cache.enabled and cache.client else None


# DALTON-HOOK(catalog-cache): read raw cached entry to allow preserving last successful list on failure
async def _cache_read_raw(user_id: str, provider: str, fingerprint: str) -> dict | None:
    client = _redis()
    if client is None:
        return None
    try:
        raw = await client.hget(available_models_key(user_id), provider)
        if raw is None:
            return None
        entry = json.loads(raw)
    except Exception:
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("schema") != _CACHE_SCHEMA
        or entry.get("fingerprint") != fingerprint
    ):
        return None
    return entry


async def _cache_read(user_id: str, provider: str, fingerprint: str) -> dict | None:
    entry = await _cache_read_raw(user_id, provider, fingerprint)
    if entry is None:
        return None
    now = time.time()
    # DALTON-HOOK(catalog-cache): serve fresh list OR active short-term failure cache entry
    failed_at = entry.get("failed_at")
    if isinstance(failed_at, (int, float)) and now - failed_at <= _FAILURE_CACHE_TTL_S:
        return entry
    if isinstance(entry.get("models"), list) and now - entry.get("stored", 0) <= CACHE_TTL_S:
        return entry
    return None


async def _cache_write(user_id: str, provider: str, entry: dict) -> None:
    client = _redis()
    if client is None:
        return
    key = available_models_key(user_id)
    try:
        # DALTON-HOOK(catalog-cache): keep caller-provided stored timestamp when preserving stale list on failure
        payload = {"stored": time.time(), **entry, "schema": _CACHE_SCHEMA}
        await client.hset(key, provider, json.dumps(payload))
        await client.expire(key, _STALE_RETENTION_S)
    except Exception:
        logger.debug("[provider_models] cache write failed", exc_info=True)


# DALTON-HOOK(catalog-cache): generation-checked cache writes so concurrent refreshes
# cannot overwrite a fresher successful list with an older stale list or failure record.
def _cache_lock(user_id: str, provider: str) -> asyncio.Lock:
    lock_key = (available_models_key(user_id), provider)
    lock = _cache_write_locks.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _cache_write_locks[lock_key] = lock
    return lock


def _entry_gen(entry: dict | None) -> int:
    if not isinstance(entry, dict):
        return 0
    raw = entry.get("gen")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


# DALTON-HOOK(catalog-cache): cross-worker atomic Redis Lua CAS for model-list cache writes
_CAS_SUCCESS_LUA = """
local key = KEYS[1]
local field = ARGV[1]
local schema = tonumber(ARGV[2])
local fp = ARGV[3]
local base_gen = tonumber(ARGV[4])
local shape = ARGV[5]
local models = cjson.decode(ARGV[6])
local fetched_at = ARGV[7]
local now_ts = tonumber(ARGV[8])
local ttl_s = tonumber(ARGV[9])

local raw = redis.call("HGET", key, field)
local cur_gen = 0
if raw then
  local ok, cur = pcall(cjson.decode, raw)
  if ok and type(cur) == "table" and cur.schema == schema and cur.fingerprint == fp then
    if type(cur.gen) == "number" and cur.gen >= 0 then
      cur_gen = math.floor(cur.gen)
    end
    if cur_gen > base_gen and type(cur.models) == "table" and type(cur.fetched_at) == "string" and cur.fetched_at > fetched_at then
      return raw
    end
  end
end

if next(models) == nil then
  models = cjson.empty_array
end
local next_gen = math.max(cur_gen, base_gen) + 1
local out = cjson.encode({
  schema = schema,
  fingerprint = fp,
  shape = shape,
  models = models,
  fetched_at = fetched_at,
  stored = now_ts,
  gen = next_gen,
})
redis.call("HSET", key, field, out)
redis.call("EXPIRE", key, ttl_s)
return out
"""

_CAS_FAILURE_LUA = """
local key = KEYS[1]
local field = ARGV[1]
local schema = tonumber(ARGV[2])
local fp = ARGV[3]
local base_gen = tonumber(ARGV[4])
local req_shape = ARGV[5]
local reason = ARGV[6]
local now_ts = tonumber(ARGV[7])
local ttl_s = tonumber(ARGV[8])
local fb_raw = ARGV[9]

local raw = redis.call("HGET", key, field)
local cur = nil
local cur_gen = 0
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if ok and type(decoded) == "table" and decoded.schema == schema and decoded.fingerprint == fp then
    cur = decoded
    if type(cur.gen) == "number" and cur.gen >= 0 then
      cur_gen = math.floor(cur.gen)
    end
    if cur_gen > base_gen and type(cur.models) == "table" then
      return raw
    end
  end
end

local preserved = nil
if cur and type(cur.models) == "table" then
  preserved = cur
elseif fb_raw ~= "" then
  local ok_fb, fb = pcall(cjson.decode, fb_raw)
  if ok_fb and type(fb) == "table" and type(fb.models) == "table" then
    preserved = fb
  end
end

local out_tbl
if preserved then
  local p_gen = cur_gen
  if type(preserved.gen) == "number" and preserved.gen > p_gen then
    p_gen = math.floor(preserved.gen)
  end
  local p_models = preserved.models
  if next(p_models) == nil then
    p_models = cjson.empty_array
  end
  out_tbl = {
    schema = schema,
    fingerprint = fp,
    shape = preserved.shape or req_shape,
    models = p_models,
    fetched_at = preserved.fetched_at,
    stored = preserved.stored or 0,
    gen = p_gen,
    failed_at = now_ts,
    failure_reason = reason,
  }
else
  out_tbl = {
    schema = schema,
    fingerprint = fp,
    shape = req_shape,
    models = cjson.null,
    fetched_at = cjson.null,
    stored = 0,
    gen = cur_gen,
    failed_at = now_ts,
    failure_reason = reason,
  }
end
local out = cjson.encode(out_tbl)
redis.call("HSET", key, field, out)
redis.call("EXPIRE", key, ttl_s)
return out
"""


_DEFAULT_CACHE_WRITE = _cache_write


async def _cache_write_success(
    user_id: str,
    provider: str,
    fingerprint: str,
    *,
    base_gen: int,
    shape: str,
    models: list[dict],
    fetched_at: str,
) -> tuple[str, list[dict], str]:
    client = _redis()
    if (
        _cache_write is _DEFAULT_CACHE_WRITE
        and client is not None
        and callable(getattr(client, "eval", None))
    ):
        key = available_models_key(user_id)
        try:
            raw = await client.eval(
                _CAS_SUCCESS_LUA,
                1,
                key,
                provider,
                _CACHE_SCHEMA,
                fingerprint,
                base_gen,
                shape,
                json.dumps(models),
                fetched_at,
                time.time(),
                _STALE_RETENTION_S,
            )
            if raw is not None:
                winner = json.loads(raw)
                if isinstance(winner, dict) and isinstance(winner.get("models"), list):
                    return (
                        str(winner.get("shape") or shape),
                        winner["models"],
                        str(winner.get("fetched_at") or fetched_at),
                    )
        except Exception:
            logger.debug("[provider_models] atomic success cache write failed", exc_info=True)
            return shape, models, fetched_at

    async with _cache_lock(user_id, provider):
        current = await _cache_read_raw(user_id, provider, fingerprint)
        current_gen = _entry_gen(current)
        if (
            current is not None
            and current_gen > base_gen
            and isinstance(current.get("models"), list)
            and str(current.get("fetched_at") or "") > fetched_at
        ):
            return (
                str(current.get("shape") or shape),
                current["models"],
                str(current.get("fetched_at") or fetched_at),
            )
        next_gen = max(current_gen, base_gen) + 1
        await _cache_write(
            user_id,
            provider,
            {
                "fingerprint": fingerprint,
                "shape": shape,
                "models": models,
                "fetched_at": fetched_at,
                "gen": next_gen,
            },
        )
        return shape, models, fetched_at


async def _cache_write_failure(
    user_id: str,
    provider: str,
    fingerprint: str,
    *,
    base_gen: int,
    req_shape: str,
    reason: str,
    fallback_stale: dict | None,
) -> dict | None:
    """Record a short-term failure without clobbering a newer concurrent success.

    Uses atomic Redis Lua CAS when available so cross-worker concurrent refreshes
    cannot roll back a newer generation. Returns the preserved successful entry
    when one exists, or None when no successful list exists.
    """
    client = _redis()
    if (
        _cache_write is _DEFAULT_CACHE_WRITE
        and client is not None
        and callable(getattr(client, "eval", None))
    ):
        key = available_models_key(user_id)
        fb_json = (
            json.dumps(fallback_stale)
            if isinstance(fallback_stale, dict) and isinstance(fallback_stale.get("models"), list)
            else ""
        )
        try:
            raw = await client.eval(
                _CAS_FAILURE_LUA,
                1,
                key,
                provider,
                _CACHE_SCHEMA,
                fingerprint,
                base_gen,
                req_shape,
                reason,
                time.time(),
                _STALE_RETENTION_S,
                fb_json,
            )
            if raw is not None:
                entry = json.loads(raw)
                if isinstance(entry, dict) and isinstance(entry.get("models"), list):
                    return entry
            return None
        except Exception:
            logger.debug("[provider_models] atomic failure cache write failed", exc_info=True)
            if isinstance(fallback_stale, dict) and isinstance(fallback_stale.get("models"), list):
                return fallback_stale
            return None

    async with _cache_lock(user_id, provider):
        current = await _cache_read_raw(user_id, provider, fingerprint)
        current_gen = _entry_gen(current)
        if (
            current is not None
            and current_gen > base_gen
            and isinstance(current.get("models"), list)
        ):
            # A newer concurrent refresh already succeeded — leave it untouched.
            return current
        preserved = (
            current
            if current is not None and isinstance(current.get("models"), list)
            else (
                fallback_stale
                if isinstance(fallback_stale, dict)
                and isinstance(fallback_stale.get("models"), list)
                else None
            )
        )
        if preserved is not None:
            preserved_entry = {
                "fingerprint": fingerprint,
                "shape": preserved.get("shape") or req_shape,
                "models": preserved["models"],
                "fetched_at": preserved.get("fetched_at"),
                "stored": preserved.get("stored", 0),
                "gen": max(current_gen, _entry_gen(preserved)),
                "failed_at": time.time(),
                "failure_reason": reason,
            }
            await _cache_write(user_id, provider, preserved_entry)
            return preserved_entry
        await _cache_write(
            user_id,
            provider,
            {
                "fingerprint": fingerprint,
                "shape": req_shape,
                "models": None,
                "fetched_at": None,
                "stored": 0,
                "gen": current_gen,
                "failed_at": time.time(),
                "failure_reason": reason,
            },
        )
        return None


async def _refresh_allowed(user_id: str, provider: str) -> bool:
    """One forced refresh per user and provider every few seconds, so the
    button cannot be turned into a stream of calls to the provider."""
    from src.utils.cache.throttle import throttle

    return await throttle(f"provider_models:refresh:{user_id}:{provider}", 1, _REFRESH_COOLDOWN_S)


#: How long a catalog that could not be read is taken as unreadable.
_CATALOG_RETRY_S = 300


async def _gateway_catalog(info: dict) -> GatewayCatalog | None:
    """A gateway's catalog (``model_catalog``), its section shared by every
    user for as long as a list is kept; None for any other provider."""
    config = info.get("model_catalog")
    if not config:
        return None
    url_key = hashlib.sha256(config["url"].encode()).hexdigest()[:16]
    key = f"model_catalog:{url_key}:{config['provider']}"
    client = _redis()
    if client is not None:
        try:
            raw = await client.get(key)
            if raw is not None:
                return GatewayCatalog(config, json.loads(raw))
        except Exception:
            logger.debug("[provider_models] catalog cache read failed", exc_info=True)
    section = await fetch_section(config)
    if client is not None:
        # A catalog that could not be read is kept briefly too, so an outage
        # costs one wait per few minutes rather than one per list.
        try:
            await client.set(key, json.dumps(section), ex=CACHE_TTL_S if section is not None else _CATALOG_RETRY_S)
        except Exception:
            logger.debug("[provider_models] catalog cache write failed", exc_info=True)
    return GatewayCatalog(config, section)


async def invalidate_available_models(user_id: str) -> None:
    """Drop every cached list of the user's. Called when a key or account is
    saved or removed: a key under one slug can serve its siblings' lists."""
    client = _redis()
    if client is None:
        return
    try:
        await client.delete(available_models_key(user_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def available_models(user_id: str, provider: str, *, refresh: bool = False, q: str | None = None) -> ModelsResult:
    """What the user's credential for ``provider`` can use; see the package doc.

    Raises ``UnknownProvider``, ``UnsupportedProvider`` or ``NotConnected``.
    """
    from src.llms.llm import LLM
    from src.server.services.llm import egress

    mc = LLM.get_model_config()
    info = mc.get_provider_info(provider)
    if not info:
        raise UnknownProvider(provider)
    if info.get("platform") or info.get("dynamic_models") or info.get("access_type") == "local":
        raise UnsupportedProvider(provider)

    shape = list_shape(info)
    try:
        cred = await _resolve_credential(user_id, provider, mc)
    except _OAuthUnavailable as e:
        return filter_others({**build_result(provider, shape, None, mc, reason=e.reason), "cached": False}, q)

    match_kwargs = {"holder": cred.holder, "base_url_saved": bool(cred.base_url)}
    if shape == CODEX:
        from src.llms.extension.codex import codex_client_version

        extra = [codex_client_version()]
    else:
        extra = []
    fingerprint = cred.fingerprint(provider, extra)

    if not refresh or not await _refresh_allowed(user_id, provider):
        entry = await _cache_read(user_id, provider, fingerprint)
        if entry is not None:
            # DALTON-HOOK(catalog-cache): return cached list (fresh or stale-preserved) or cached failure
            cached_models = entry.get("models")
            if isinstance(cached_models, list):
                result = build_result(
                    provider, entry["shape"], cached_models, mc, fetched_at=entry.get("fetched_at"),
                    catalog=await _gateway_catalog(info), **match_kwargs,
                )
                return filter_others({**result, "cached": True}, q)
            cached_reason = entry.get("failure_reason")
            result = build_result(
                provider, entry["shape"], None, mc, reason=cached_reason,
                catalog=await _gateway_catalog(info) if cached_reason != UNAUTHORIZED else None,
                **match_kwargs,
            )
            return filter_others({**result, "cached": True}, q)

    # DALTON-HOOK(catalog-cache): capture stale entry & generation before fetching for CAS write
    stale_entry = await _cache_read_raw(user_id, provider, fingerprint)
    base_gen = _entry_gen(stale_entry)
    req = build_list_request(info, api_key=cred.api_key, base_url=cred.base_url, token_headers=cred.token_headers)
    started = time.monotonic()
    # A gateway's catalog is read beside its list: neither waits on the other.
    catalog_read = asyncio.create_task(_gateway_catalog(info))
    try:
        # A list on a base URL the user saved goes out as a key test does.
        # DALTON-HOOK(team-model-catalog): catalog-defined variants always send model list
        # fetches through CatalogVariantTransport (Fake-IP DoH / HeldTransport + bound URL check),
        # using bound_base_url & key_fingerprint from the SAME credential read that built req.
        if info.get("catalog_defined") is True:
            from src.dalton.services.team_model_catalog.constants import make_key_fingerprint
            from src.dalton.services.team_model_catalog.transport import (
                make_catalog_variant_http_transport,
            )

            bound_url = str(cred.base_url or "").strip()
            expected_fp = (
                str(cred.catalog_key_fingerprint).strip()
                if cred.catalog_key_fingerprint
                else (make_key_fingerprint(cred.api_key) if cred.api_key else "")
            )
            if not bound_url or not expected_fp:
                raise ListFetchError(ENDPOINT_REFUSED)
            transport = make_catalog_variant_http_transport(
                provider_slug=provider,
                bound_base_url=bound_url,
                expected_key_fingerprint=expected_fp,
                verify_db_binding=True,
            )
        else:
            # DALTON-HOOK(standalone-core): route through address-based pinned egress transport in core local auth
            try:
                transport = await egress.user_endpoint_transport(cred.base_url)
            except egress.EndpointRefused as e:
                raise ListFetchError(ENDPOINT_REFUSED) from e
        models = normalize(req.shape, await fetch_list(req, transport=transport))
        # DALTON-HOOK(catalog-cache): empty Antigravity 200 payload is treated as bad_response so prior list is preserved
        if req.shape == ANTIGRAVITY and not models:
            raise ListFetchError(BAD_RESPONSE)
    except ListFetchError as e:
        catalog = await catalog_read
        logger.warning(
            "[provider_models] list fetch failed provider=%s user_id=%s reason=%s status=%s",
            provider, user_id, e.reason, e.status,
        )
        reason = e.reason
        # A token that just passed the refresh check, refused for the plan
        # rather than the sign-in (live-checked on xAI's 403). The message
        # tells the two apart: xAI also answers a stale token with a 403.
        subscription = SUBSCRIPTIONS.get(provider)
        if subscription is not None and subscription.plan_refused(e.status, e.detail):
            reason = NO_API_ACCESS
        elif subscription is not None and subscription.token_refused(e.status, e.detail):
            # Revoked before its stated expiry: spent now, so the next list or
            # turn refreshes it instead of offering it again until then.
            await _expire_refused_token(user_id, provider)
        # DALTON-HOOK(catalog-cache): short-term cache transient failures and preserve last successful list/fetched_at
        if reason not in (UNAUTHORIZED, NO_API_ACCESS):
            preserved = await _cache_write_failure(
                user_id,
                provider,
                fingerprint,
                base_gen=base_gen,
                req_shape=req.shape,
                reason=reason,
                fallback_stale=stale_entry,
            )
            if preserved is not None:
                result = build_result(
                    provider,
                    preserved.get("shape") or req.shape,
                    preserved["models"],
                    mc,
                    fetched_at=preserved.get("fetched_at"),
                    catalog=catalog,
                    **match_kwargs,
                )
                return filter_others({**result, "cached": True}, q)
        # A refused key runs none of a gateway's models, so its catalog does
        # not stand in for the list then.
        result = build_result(
            provider, req.shape, None, mc, reason=reason,
            catalog=catalog if reason != UNAUTHORIZED else None, **match_kwargs,
        )
        return filter_others({**result, "cached": False}, q)
    except BaseException:
        catalog_read.cancel()
        raise
    catalog = await catalog_read

    fetched_at = datetime.now(timezone.utc).isoformat()
    # DALTON-HOOK(catalog-cache): generation-checked success write
    final_shape, final_models, final_fetched_at = await _cache_write_success(
        user_id,
        provider,
        fingerprint,
        base_gen=base_gen,
        shape=req.shape,
        models=models,
        fetched_at=fetched_at,
    )
    result = build_result(
        provider, final_shape, final_models, mc, fetched_at=final_fetched_at, catalog=catalog, **match_kwargs
    )
    if result["status"] == "fallback":
        logger.warning(
            "[provider_models] falling back to the manifest provider=%s user_id=%s reason=%s listed=%d",
            provider, user_id, result["reason"], len(models),
        )
    logger.debug(
        "[provider_models] listed provider=%s models=%d ready=%d in %dms",
        provider, len(models), len(result["ready"]), int((time.monotonic() - started) * 1000),
    )
    return filter_others({**result, "cached": False}, q)


async def _expire_refused_token(user_id: str, provider: str) -> None:
    from src.server.database.oauth_tokens import expire_oauth_access_token

    try:
        await expire_oauth_access_token(user_id, provider)
    except Exception:
        logger.warning("[provider_models] could not expire a refused token provider=%s", provider, exc_info=True)


#: A custom endpoint's API format -> the SDK family whose list shape it answers in.
FORMAT_SDKS = {
    "openai-responses": "openai",
    "openai-completions": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
}


async def endpoint_models(api_format: str, base_url: str, api_key: str, *, transport: Any = None) -> dict:
    """What a custom endpoint lists, read with the user's key before anything is saved.

    ``status: "unavailable"`` with a ``reason`` when the list could not be
    read: the endpoint may serve no list at all, and the user can still name a
    model by hand.
    """
    from src.llms.llm import LLM as LLMFactory

    req = build_list_request({"sdk": FORMAT_SDKS[api_format]}, api_key=api_key, base_url=base_url)
    try:
        # A 200 that is not a list (z.ai's Anthropic route answers one) fails here.
        models = normalize(req.shape, await fetch_list(req, transport=transport))
    except ListFetchError as e:
        return {"status": "unavailable", "reason": e.reason, "recognized": [], "others": []}
    mc = LLMFactory.get_model_config()
    route_api = wire_api(mc.get_provider_info(FORMAT_SDKS[api_format]), api_format == "openai-responses")
    return {"status": "ok", **match_endpoint_models(models, mc, route_api=route_api)}


async def local_server_models(base_url: str, api_key: str | None, *, transport: Any = None) -> list[dict]:
    """``[{id, name}]`` a local server (Ollama, LM Studio, vLLM) lists, unfiltered.

    Each serves an OpenAI-shaped ``GET /v1/models`` whatever SDK runs its
    inference, and its base URL may or may not already end in ``/v1``.
    Raises ``ListFetchError``.
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    req = build_list_request({"sdk": OPENAI}, api_key=api_key, base_url=base)
    payload = await fetch_list(req, transport=transport, timeout=10.0)
    data = payload.get("data") if isinstance(payload, dict) else None
    return [
        {"id": m["id"], "name": m["id"]}
        for m in data or []
        if isinstance(m, dict) and m.get("id")
    ]
