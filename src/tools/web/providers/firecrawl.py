"""Firecrawl provider: single-page scrape (/v2/scrape) fetch adapter and
site-crawl adapter (/v2/crawl async jobs + /v2/map URL discovery).

API docs: https://docs.firecrawl.dev — Bearer auth (FIRECRAWL_API_KEY).
Set FIRECRAWL_BASE_URL to target a self-hosted instance. The ``proxy``
native param ("auto" | "stealth") comes from the manifest level — the
"stealth" level is the router's anti-bot escalation target.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from src.tools.web.providers._shared import (
    clip_error,
    empty_content_error,
    error_from_httpx,
    error_response,
    is_empty_markdown,
    missing_key_error,
    redact_request_headers,
    request_json,
    # DALTON-HOOK(firecrawl-budget): multi-key failover
    sanitize_httpx_error,
)
from src.tools.web.types import (
    CrawlJob,
    CrawlPage,
    CrawlRequest,
    CrawlState,
    CrawlStatus,
    FetchRequest,
    FetchResponse,
    FetchResult,
    MapRequest,
    UrlInfo,
    WebError,
    WebErrorType,
    WebToolError,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.firecrawl.dev"
_TIMEOUT = 90.0  # stealth proxy scrapes can be slow
_MAX_STATUS_PAGES = 20  # crawl-status pagination hop cap (crawls are ≤100 pages)
# Firecrawl's maxAge defaults to 2 DAYS when omitted; bound provider-cache
# staleness to WebFetch's own Redis cache window instead.
_DEFAULT_MAX_AGE_SECONDS = 900

# DALTON-HOOK(firecrawl-budget): multi-key primary/backup failover & cooldown constants.
_DEFAULT_COOLDOWN_SECONDS = 24 * 60 * 60
_MISSING_BALANCE_COOLDOWN_SECONDS = 60 * 60
_EXPIRED_RESET_COOLDOWN_SECONDS = 5 * 60
_COOLDOWN_REDIS_PREFIX = "firecrawl:key_cooldown:"
_CRAWL_KEY_REDIS_PREFIX = "firecrawl:crawl_key:"
_CRAWL_KEY_TTL_SECONDS = 24 * 60 * 60
_MAX_INMEM_CRAWL_KEYS = 1024

_INMEM_COOLDOWNS: Dict[str, Dict[str, Any]] = {}
_INMEM_CRAWL_KEYS: "OrderedDict[str, str]" = OrderedDict()
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

# Atomic compare-and-set: keep whichever cooldown_until is later so an in-flight
# worker with a shorter fallback cannot overwrite a longer explicit resets_at.
_EXTEND_COOLDOWN_LUA = """
local kept = redis.call('get', KEYS[1])
if kept then
  local ok, t = pcall(cjson.decode, kept)
  if ok and type(t) == 'table' and tonumber(t.cooldown_until) and tonumber(t.cooldown_until) >= tonumber(ARGV[3]) then
    return kept
  end
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
return nil
"""

_last_request_key_fp: ContextVar[Optional[str]] = ContextVar(
    "_last_request_key_fp", default=None
)
_pinned_request_key_fp: ContextVar[Optional[str]] = ContextVar(
    "_pinned_request_key_fp", default=None
)


def load_firecrawl_api_keys() -> List[str]:
    """Load Firecrawl API keys in priority order: FIRECRAWL_API_KEY, then
    FIRECRAWL_API_KEY_2, FIRECRAWL_API_KEY_3, ... stopping at the first gap."""
    keys: List[str] = []
    first = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if not first:
        return keys
    keys.append(first)
    idx = 2
    while True:
        nxt = (os.getenv(f"FIRECRAWL_API_KEY_{idx}") or "").strip()
        if not nxt:
            break
        keys.append(nxt)
        idx += 1
    return list(dict.fromkeys(keys))


def _key_fingerprint(api_key: str) -> str:
    """Short non-reversible fingerprint (12-char SHA-256 hex prefix) for logs & Redis."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _sanitize_http_error(
    err: httpx.HTTPError, api_keys: Sequence[str] = ()
) -> httpx.HTTPError:
    """Return a newly built httpx error whose request headers and message contain no raw keys."""
    return sanitize_httpx_error(err, scrub_values=api_keys)


def _format_utc_iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _compute_cooldown_until(
    resets_at: Optional[str],
    now: Optional[float] = None,
    *,
    remaining: Optional[float] = None,
) -> float:
    """Resolve cooldown deadline from ISO ``resets_at`` and ``remaining`` credits.

    - Future timestamp -> cool down until ``resets_at``.
    - Already-passed timestamp -> short cooldown (``_EXPIRED_RESET_COOLDOWN_SECONDS``).
    - Missing or unparseable:
      - explicit ``remaining <= 0`` -> 24h fallback (``_DEFAULT_COOLDOWN_SECONDS``).
      - missing ``remaining`` -> 1h fallback (``_MISSING_BALANCE_COOLDOWN_SECONDS``).
    """
    current = time.time() if now is None else now
    if resets_at:
        try:
            dt = datetime.fromisoformat(resets_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            target = dt.timestamp()
            if target > current:
                return target
            return current + _EXPIRED_RESET_COOLDOWN_SECONDS
        except ValueError:
            pass
    if remaining is not None and remaining <= 0:
        return current + _DEFAULT_COOLDOWN_SECONDS
    return current + _MISSING_BALANCE_COOLDOWN_SECONDS


def _get_ready_redis_cache():
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if cache.enabled and cache.client:
            return cache
    except Exception:
        logger.debug("Redis cache unavailable for Firecrawl key state", exc_info=True)
    return None


def _merge_local_cooldown(fp: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Keep whichever cooldown entry for ``fp`` has the later ``cooldown_until``."""
    existing = _INMEM_COOLDOWNS.get(fp)
    if existing is not None:
        existing_until = float(existing.get("cooldown_until", 0) or 0)
        candidate_until = float(candidate.get("cooldown_until", 0) or 0)
        if existing_until >= candidate_until:
            return existing
    _INMEM_COOLDOWNS[fp] = candidate
    return candidate


async def _get_key_cooldown(fp: str) -> Optional[Dict[str, Any]]:
    """Return active cooldown payload for key fingerprint ``fp`` by merging Redis and local state."""
    now = time.time()
    local_entry = _INMEM_COOLDOWNS.get(fp)
    if local_entry is not None and float(local_entry.get("cooldown_until", 0) or 0) <= now:
        _INMEM_COOLDOWNS.pop(fp, None)
        local_entry = None

    redis_entry: Optional[Dict[str, Any]] = None
    redis_key = f"{_COOLDOWN_REDIS_PREFIX}{fp}"
    cache = _get_ready_redis_cache()
    if cache is not None:
        try:
            read_fn = getattr(cache, "get_strict", None) or cache.get
            data = await read_fn(redis_key)
            if isinstance(data, dict) and float(data.get("cooldown_until", 0) or 0) > now:
                redis_entry = data
        except Exception:
            logger.debug(
                "Redis read failed for Firecrawl cooldown fp=%s; using local record",
                fp,
                exc_info=True,
            )

    if local_entry is not None and redis_entry is not None:
        best = max(
            (local_entry, redis_entry),
            key=lambda item: float(item.get("cooldown_until", 0) or 0),
        )
    else:
        best = redis_entry or local_entry
    if best is not None:
        _INMEM_COOLDOWNS[fp] = best
    return best


async def _mark_key_cooldown(
    fp: str, *, resets_at: Optional[str] = None, remaining: Optional[float] = None
) -> Dict[str, Any]:
    """Record cooldown for key fingerprint ``fp`` in local memory and Redis, keeping the later deadline."""
    now = time.time()
    until = _compute_cooldown_until(resets_at, now, remaining=remaining)
    payload: Dict[str, Any] = {
        "fingerprint": fp,
        "cooldown_until": until,
        "resets_at": resets_at,
        "marked_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "reason": "insufficient_credits",
    }
    effective = _merge_local_cooldown(fp, payload)
    effective_until = float(effective.get("cooldown_until", until) or until)
    ttl = max(1, math.ceil(effective_until - now))
    redis_key = f"{_COOLDOWN_REDIS_PREFIX}{fp}"
    cache = _get_ready_redis_cache()
    if cache is not None:
        try:
            eval_fn = getattr(cache.client, "eval", None)
            if callable(eval_fn):
                kept_raw = await eval_fn(
                    _EXTEND_COOLDOWN_LUA,
                    1,
                    redis_key,
                    json.dumps(effective, ensure_ascii=False),
                    ttl,
                    repr(effective_until),
                )
                if isinstance(kept_raw, (bytes, bytearray)):
                    kept_raw = kept_raw.decode("utf-8")
                if isinstance(kept_raw, str) and kept_raw:
                    try:
                        kept_data = json.loads(kept_raw)
                        if isinstance(kept_data, dict):
                            effective = _merge_local_cooldown(fp, kept_data)
                    except ValueError:
                        pass
                elif isinstance(kept_raw, dict):
                    effective = _merge_local_cooldown(fp, kept_raw)
            else:
                read_fn = getattr(cache, "get_strict", None) or cache.get
                existing_data = await read_fn(redis_key)
                if (
                    isinstance(existing_data, dict)
                    and float(existing_data.get("cooldown_until", 0) or 0) >= effective_until
                ):
                    effective = _merge_local_cooldown(fp, existing_data)
                else:
                    ok = bool(await cache.set(redis_key, effective, ttl=ttl))
                    if not ok:
                        logger.warning(
                            "Redis set returned False for Firecrawl cooldown fp=%s; kept local record",
                            fp,
                        )
        except Exception:
            logger.warning(
                "Redis write failed for Firecrawl cooldown fp=%s; kept local record",
                fp,
                exc_info=True,
            )
    return effective


def _cache_crawl_key_in_memory(crawl_id: str, fp: str) -> None:
    _INMEM_CRAWL_KEYS[crawl_id] = fp
    _INMEM_CRAWL_KEYS.move_to_end(crawl_id)
    while len(_INMEM_CRAWL_KEYS) > _MAX_INMEM_CRAWL_KEYS:
        _INMEM_CRAWL_KEYS.popitem(last=False)


async def _write_crawl_key_to_redis(crawl_id: str, fp: str) -> None:
    cache = _get_ready_redis_cache()
    if cache is None:
        return
    try:
        ok = bool(
            await cache.set(
                f"{_CRAWL_KEY_REDIS_PREFIX}{crawl_id}",
                {"fingerprint": fp},
                ttl=_CRAWL_KEY_TTL_SECONDS,
            )
        )
        if not ok:
            logger.warning(
                "Redis set returned False for Firecrawl crawl key map crawl_id=%s fp=%s",
                crawl_id,
                fp,
            )
    except Exception:
        logger.warning(
            "Redis write failed for Firecrawl crawl key map crawl_id=%s fp=%s",
            crawl_id,
            fp,
            exc_info=True,
        )


def _schedule_crawl_key_record(crawl_id: str, fp: str) -> None:
    """Record ``crawl_id -> fp`` locally at once and persist to Redis asynchronously."""
    if not crawl_id or not fp:
        return
    _cache_crawl_key_in_memory(crawl_id, fp)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_write_crawl_key_to_redis(crawl_id, fp))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _get_crawl_key(crawl_id: str) -> Optional[str]:
    """Look up the originating key fingerprint for ``crawl_id``."""
    if not crawl_id:
        return None
    fp = _INMEM_CRAWL_KEYS.get(crawl_id)
    if fp:
        return fp
    cache = _get_ready_redis_cache()
    if cache is not None:
        try:
            read_fn = getattr(cache, "get_strict", None) or cache.get
            data = await read_fn(f"{_CRAWL_KEY_REDIS_PREFIX}{crawl_id}")
            if isinstance(data, dict) and isinstance(data.get("fingerprint"), str):
                fp = data["fingerprint"]
                _cache_crawl_key_in_memory(crawl_id, fp)
                return fp
        except Exception:
            logger.warning(
                "Redis read failed for Firecrawl crawl key map crawl_id=%s",
                crawl_id,
                exc_info=True,
            )
    return None


def _earliest_reset_display(cooldowns: Sequence[Dict[str, Any]]) -> Optional[str]:
    if not cooldowns:
        return None
    earliest = min(cooldowns, key=lambda cd: float(cd.get("cooldown_until", 0) or 0))
    until = float(earliest.get("cooldown_until", 0) or 0)
    if until <= 0:
        return None
    return _format_utc_iso(until)


async def get_firecrawl_pool_cooldown_state(
    api_keys: Optional[Sequence[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Return ``(all_in_cooldown, earliest_resets_at)`` when multiple Firecrawl keys exist.

    Single-key configs return ``(False, None)`` so existing router/breaker
    behavior stays unchanged.
    """
    keys = list(api_keys) if api_keys is not None else load_firecrawl_api_keys()
    if len(keys) <= 1:
        return False, None
    cooldowns: List[Dict[str, Any]] = []
    for key in keys:
        cd = await _get_key_cooldown(_key_fingerprint(key))
        if cd is None:
            return False, None
        cooldowns.append(cd)
    return True, _earliest_reset_display(cooldowns)


# Firecrawl returns 408 for scrape-engine timeouts.
_STATUS_OVERRIDES = {408: WebErrorType.TIMEOUT}


# DALTON-HOOK(firecrawl-budget): extract reset timestamp and remaining credits for 402 insufficient_credits.
def _extract_resets_at(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    for container in (
        body,
        body.get("data"),
        body.get("details"),
        body.get("meta"),
        body.get("error") if isinstance(body.get("error"), dict) else None,
    ):
        if not isinstance(container, dict):
            continue
        for key in ("resets_at", "resetsAt", "reset_at", "resetAt"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _extract_remaining_credits(body: Any) -> Optional[float]:
    if not isinstance(body, dict):
        return None
    for container in (
        body,
        body.get("data"),
        body.get("details"),
        body.get("meta"),
        body.get("error") if isinstance(body.get("error"), dict) else None,
    ):
        if not isinstance(container, dict):
            continue
        for key in ("remaining_credits", "remainingCredits"):
            val = container.get(key)
            if isinstance(val, bool) or val is None:
                continue
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str) and val.strip():
                try:
                    return float(val.strip())
                except ValueError:
                    continue
    return None


def _parse_402_credit_info(
    e: httpx.HTTPStatusError,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str], Optional[float]]:
    if e.response.status_code != 402:
        return False, None, None, None
    try:
        body = e.response.json()
    except ValueError:
        body = None
    code = ""
    if isinstance(body, dict):
        raw_code = body.get("code")
        if not raw_code and isinstance(body.get("error"), dict):
            raw_code = body["error"].get("code")
        code = str(raw_code or "").strip().lower()
    raw_text = (e.response.text or "").lower()
    resets_at = _extract_resets_at(body)
    if code:
        is_insufficient = code == "insufficient_credits"
    else:
        is_insufficient = (
            "platform_managed_credits" not in raw_text
            and (
                "insufficient_credits" in raw_text
                or "insufficient credits" in raw_text
                or resets_at is not None
            )
        )
    if not is_insufficient:
        return False, body if isinstance(body, dict) else None, resets_at, None
    remaining = _extract_remaining_credits(body)
    return True, body if isinstance(body, dict) else None, resets_at, remaining


_PER_REQUEST_CREDIT_MARKERS = (
    "to perform this request",
    "for this request",
    "perform this request",
    "to perform this scrape",
    "for this scrape",
    "to perform this crawl",
    "for this crawl",
    "not enough credits for this",
    "exceeds remaining credits",
    "exceeds your remaining credits",
)


def _is_per_request_credit_error(body: Any, raw_text: str) -> bool:
    candidates = [raw_text or ""]
    if isinstance(body, dict):
        err_val = body.get("error")
        if isinstance(err_val, str):
            candidates.append(err_val)
        elif isinstance(err_val, dict) and isinstance(err_val.get("message"), str):
            candidates.append(err_val["message"])
        if isinstance(body.get("message"), str):
            candidates.append(body["message"])
    combined = " ".join(candidates).lower()
    return any(marker in combined for marker in _PER_REQUEST_CREDIT_MARKERS)


def _classify_402_credit_info(
    e: httpx.HTTPStatusError,
) -> Tuple[bool, bool, Optional[Dict[str, Any]], Optional[str], Optional[float]]:
    is_insufficient, body, resets_at, remaining = _parse_402_credit_info(e)
    if not is_insufficient:
        return False, False, body, resets_at, None
    is_per_request = (remaining is not None and remaining > 0) or (
        remaining is None and _is_per_request_credit_error(body, e.response.text or "")
    )
    return True, is_per_request, body, resets_at, remaining


def _firecrawl_402_error(
    e: httpx.HTTPStatusError, *, multi_key: Optional[bool] = None
) -> Optional[WebError]:
    from src.dalton.settings import is_dalton_enabled

    if e.response.status_code != 402 or not is_dalton_enabled():
        return None
    is_insufficient, is_per_request, body, resets_at, remaining = _classify_402_credit_info(e)
    if not is_insufficient:
        return None
    is_multi = (len(load_firecrawl_api_keys()) > 1) if multi_key is None else multi_key

    if not is_multi:
        if remaining is not None and remaining > 0:
            return WebError(
                type=WebErrorType.BUDGET_EXCEEDED,
                message="本次请求所需额度超过 Firecrawl 余额",
                http_status=402,
                retryable=True,
                provider_fault=True,
                native_kind="insufficient_credits_for_request",
            )
        if remaining is not None and remaining <= 0:
            reset_display = resets_at or "下个计费周期"
            return WebError(
                type=WebErrorType.BUDGET_EXCEEDED,
                message=f"Firecrawl 本月额度已用完，将于 {reset_display} 重置，请改用浏览器抓取",
                http_status=402,
                retryable=True,
                provider_fault=True,
                native_kind="insufficient_credits",
            )
        orig_err = body.get("error") if isinstance(body, dict) else None
        raw_msg = (
            orig_err.strip()
            if isinstance(orig_err, str) and orig_err.strip()
            else (e.response.text or "").strip() or "Insufficient credits"
        )
        return WebError(
            type=WebErrorType.BUDGET_EXCEEDED,
            message=f"Firecrawl 返回额度不足（402）：{clip_error(raw_msg)}",
            http_status=402,
            retryable=True,
            provider_fault=True,
            native_kind="insufficient_credits_unknown",
        )

    if is_per_request:
        return WebError(
            type=WebErrorType.PROVIDER_ERROR,
            message="本次请求所需额度超过账户余额",
            http_status=402,
            retryable=True,
            provider_fault=False,
            native_kind="insufficient_credits_for_request",
        )

    cooldown_until_raw = body.get("cooldown_until") if isinstance(body, dict) else None
    if isinstance(cooldown_until_raw, str) and cooldown_until_raw.strip():
        until_display = cooldown_until_raw.strip()
    else:
        until_display = _format_utc_iso(
            _compute_cooldown_until(resets_at, remaining=remaining)
        )
    return WebError(
        type=WebErrorType.BUDGET_EXCEEDED,
        message=f"Firecrawl 本月额度已用完，将于 {until_display} 重置，请改用浏览器抓取",
        http_status=402,
        retryable=True,
        provider_fault=True,
        native_kind="insufficient_credits",
    )


def _scrape_error(e: httpx.HTTPError, *, multi_key: Optional[bool] = None) -> WebError:
    """Map a failed /scrape call, which carries one URL.

    A 400 whose every complaint names the ``url`` field is Firecrawl refusing
    the URL itself (malformed, no top-level domain): the agent's input, which
    must not open the breaker every URL shares. Still retryable, as every
    provider-API failure is, since another provider's rules may differ.
    """
    if isinstance(e, httpx.HTTPStatusError):  # DALTON-HOOK(firecrawl-budget)
        credit_err = _firecrawl_402_error(e, multi_key=multi_key)
        if credit_err is not None:
            return credit_err
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
        try:
            body = e.response.json()
        except ValueError:
            body = None
        details = body.get("details") if isinstance(body, dict) else None
        if details and all(isinstance(d, dict) and d.get("path") == ["url"] for d in details):
            return WebError(
                type=WebErrorType.UNSUPPORTED_URL,
                message=clip_error(e.response.text),
                http_status=400,
                retryable=True,
            )
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 403:
        # A 403 from /scrape is Firecrawl declining the URL without touching
        # the target, in about 0.1s: a site it does not scrape
        # (UNSUPPORTED_SITE, often with no code), or one only an Alexandria
        # data provider serves that is off for the org
        # (THIRD_PARTY_DATA_NOT_ENABLED, e.g. Investopedia). A target's own
        # 403 arrives as a 200 with the page's status code.
        try:
            code = e.response.json().get("code")
        except (ValueError, AttributeError):
            code = None
        return WebError(
            type=WebErrorType.REFUSED,
            message=clip_error(e.response.text),
            http_status=403,
            native_kind=str(code).lower() if code else "unsupported_site",
        )
    return error_from_httpx(e, status_overrides=_STATUS_OVERRIDES)


def _target_status_error(status: int) -> Optional[WebError]:
    """Map the *scraped page's* HTTP status (data.metadata.statusCode)."""
    if status in (404, 410):
        return WebError(type=WebErrorType.NOT_FOUND, message="Page not found", http_status=status)
    if status in (401, 402, 403):
        # Target rejected the scrape — retryable so the chain can escalate
        # to a stealth level or another provider.
        return WebError(
            type=WebErrorType.ANTI_BOT, message="Target blocked the scrape", http_status=status
        )
    if status == 429:
        return WebError(
            type=WebErrorType.RATE_LIMITED, message="Target rate-limited the scrape",
            http_status=status, provider_fault=False,
        )
    if status >= 500:
        return WebError(
            type=WebErrorType.PROVIDER_ERROR, message="Target server error",
            http_status=status, provider_fault=False,
        )
    return None


class FirecrawlAPI:
    """Raw-httpx Firecrawl client. Key is read lazily so a missing
    FIRECRAWL_API_KEY surfaces as a per-call error, not a build crash."""

    # DALTON-HOOK(firecrawl-budget): multi-key primary/backup failover in _request
    # and crawl_id -> key fingerprint pinning for crawl_status / cancel_crawl.
    def __init__(self, api_key: Optional[str] = None):
        explicit = (api_key or "").strip()
        self.api_keys: List[str] = [explicit] if explicit else load_firecrawl_api_keys()
        if not self.api_keys:
            raise ValueError("FIRECRAWL_API_KEY not found in environment variables")
        self.api_key = self.api_keys[0]
        self.base_url = os.getenv("FIRECRAWL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self.headers = self._headers_for(self.api_key)

    @staticmethod
    def _headers_for(api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def scrape(
        self,
        url: str,
        proxy: Optional[str] = None,
        max_age_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /v2/scrape — one page as markdown."""
        payload: Dict[str, Any] = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
        if proxy:
            payload["proxy"] = proxy
        if max_age_seconds is None:
            max_age_seconds = _DEFAULT_MAX_AGE_SECONDS
        if max_age_seconds >= 0:
            payload["maxAge"] = max_age_seconds * 1000  # Firecrawl maxAge is ms
        return await self._request("POST", f"{self.base_url}/v2/scrape", json=payload)

    async def start_crawl(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v2/crawl — start an async crawl job; returns {id, url}."""
        # DALTON-HOOK(firecrawl-budget): multi-key failover
        if len(self.api_keys) <= 1:
            return await self._request("POST", f"{self.base_url}/v2/crawl", json=payload)
        token = _last_request_key_fp.set(None)
        try:
            data = await self._request("POST", f"{self.base_url}/v2/crawl", json=payload)
            used_fp = _last_request_key_fp.get()
        finally:
            _last_request_key_fp.reset(token)
        job_id = data.get("id")
        if data.get("success", False) and job_id and used_fp:
            _schedule_crawl_key_record(str(job_id), used_fp)
        return data

    async def _request_with_crawl_key_fallback(
        self, method: str, url: str, crawl_id: str, **kwargs: Any
    ) -> Dict[str, Any]:
        """Run a crawl status/cancel request with key pinning and 404 fallback when mapping is lost."""
        if len(self.api_keys) <= 1:
            return await self._request(method, url, **kwargs)
        pinned_fp = await _get_crawl_key(crawl_id)
        if pinned_fp and any(_key_fingerprint(k) == pinned_fp for k in self.api_keys):
            token = _pinned_request_key_fp.set(pinned_fp)
            try:
                return await self._request(method, url, **kwargs)
            finally:
                _pinned_request_key_fp.reset(token)

        last_404: Optional[httpx.HTTPStatusError] = None
        pending_err: Optional[httpx.HTTPError] = None
        for idx in range(len(self.api_keys)):
            fp = _key_fingerprint(self.api_keys[idx])
            token = _pinned_request_key_fp.set(fp)
            try:
                data = await self._request(method, url, **kwargs)
            except httpx.HTTPStatusError as e:
                sanitized_e = _sanitize_http_error(e, self.api_keys)
                assert isinstance(sanitized_e, httpx.HTTPStatusError)
                # A revoked or foreign key answers 401/403 and an unrelated
                # account 404: none of them say the job is gone, so try the next key.
                if sanitized_e.response.status_code in (401, 403, 404):
                    last_404 = sanitized_e
                    continue
                pending_err = sanitized_e
                break
            except httpx.HTTPError as e:
                pending_err = _sanitize_http_error(e, self.api_keys)
                break
            finally:
                _pinned_request_key_fp.reset(token)
            _schedule_crawl_key_record(crawl_id, fp)
            return data
        if pending_err is not None:
            raise pending_err from None
        if last_404 is not None:
            raise last_404 from None
        return {"success": False}

    async def crawl_status(
        self, crawl_id: str, skip: int = 0, page_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /v2/crawl/{id} — job status + a page window of results.

        ``page_url`` continues pagination from a response's ``next`` cursor
        (an absolute URL); otherwise ``skip`` positions the first window.
        """
        url = page_url or f"{self.base_url}/v2/crawl/{crawl_id}"
        params = {"skip": skip} if page_url is None and skip else None
        return await self._request_with_crawl_key_fallback(
            "GET", url, crawl_id, params=params
        )

    async def cancel_crawl(self, crawl_id: str) -> Dict[str, Any]:
        """DELETE /v2/crawl/{id} — cancel a running job."""
        # DALTON-HOOK(firecrawl-budget): multi-key failover
        url = f"{self.base_url}/v2/crawl/{crawl_id}"
        return await self._request_with_crawl_key_fallback("DELETE", url, crawl_id)

    async def map_site(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v2/map — discover site URLs; returns {links: [{url, ...}]}."""
        return await self._request("POST", f"{self.base_url}/v2/map", json=payload)

    @staticmethod
    def _build_all_cooled_402(
        method: str, url: str, cooldown_until: Optional[str] = None
    ) -> httpx.HTTPStatusError:
        body: Dict[str, Any] = {
            "success": False,
            "code": "insufficient_credits",
            "error": "All configured Firecrawl API keys are in credit cooldown",
            "remaining_credits": 0,
        }
        if cooldown_until:
            body["resets_at"] = cooldown_until
            body["cooldown_until"] = cooldown_until
        req = redact_request_headers(httpx.Request(method, url))
        resp = httpx.Response(402, json=body, request=req)
        return httpx.HTTPStatusError(
            "Firecrawl API error 402: All configured Firecrawl API keys are in credit cooldown",
            request=req,
            response=resp,
        )

    # DALTON-HOOK(firecrawl-budget): multi-key failover
    async def _request(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        json_body = kwargs.get("json")
        params = kwargs.get("params")

        if len(self.api_keys) == 1:
            key = self.api_keys[0]
            pending_err: Optional[httpx.HTTPError] = None
            try:
                return await request_json(
                    method,
                    url,
                    provider="Firecrawl",
                    headers=self._headers_for(key),
                    json_body=json_body,
                    params=params,
                    timeout=_TIMEOUT,
                )
            except httpx.HTTPError as e:
                pending_err = _sanitize_http_error(e, self.api_keys)
            finally:
                key = None  # type: ignore[assignment]
            if pending_err is not None:
                raise pending_err from None

        pinned_fp = _pinned_request_key_fp.get()
        if pinned_fp:
            pinned_key = next(
                (k for k in self.api_keys if _key_fingerprint(k) == pinned_fp),
                None,
            )
            if pinned_key is not None:
                pinned_err: Optional[httpx.HTTPError] = None
                try:
                    data = await request_json(
                        method,
                        url,
                        provider="Firecrawl",
                        headers=self._headers_for(pinned_key),
                        json_body=json_body,
                        params=params,
                        timeout=_TIMEOUT,
                    )
                    _last_request_key_fp.set(pinned_fp)
                    return data
                except httpx.HTTPStatusError as e:
                    sanitized_e = _sanitize_http_error(e, self.api_keys)
                    assert isinstance(sanitized_e, httpx.HTTPStatusError)
                    is_insuff, is_per_request, _, resets_at, remaining = _classify_402_credit_info(
                        sanitized_e
                    )
                    if is_insuff and not is_per_request:
                        await _mark_key_cooldown(
                            pinned_fp, resets_at=resets_at, remaining=remaining
                        )
                        logger.warning(
                            "Firecrawl pinned key fp=%s returned 402 credit exhaustion (resets_at=%s)",
                            pinned_fp,
                            resets_at or "unknown",
                        )
                    pinned_err = sanitized_e
                except httpx.HTTPError as e:
                    pinned_err = _sanitize_http_error(e, self.api_keys)
                finally:
                    pinned_key = None
                if pinned_err is not None:
                    raise pinned_err from None

        tried_fps: set[str] = set()
        last_402_error: Optional[httpx.HTTPStatusError] = None
        non_failover_err: Optional[httpx.HTTPError] = None

        async def _try_slot(
            idx: int, key: str, *, recovered: bool = False
        ) -> Tuple[bool, Optional[Dict[str, Any]]]:
            nonlocal last_402_error, non_failover_err
            fp = _key_fingerprint(key)
            if fp in tried_fps:
                return False, None
            cooldown = await _get_key_cooldown(fp)
            if cooldown is not None:
                return False, None
            tried_fps.add(fp)
            try:
                data = await request_json(
                    method,
                    url,
                    provider="Firecrawl",
                    headers=self._headers_for(key),
                    json_body=json_body,
                    params=params,
                    timeout=_TIMEOUT,
                )
                _last_request_key_fp.set(fp)
                logger.debug(
                    "Firecrawl request succeeded with %skey fp=%s (slot=%d)",
                    "recovered " if recovered else "",
                    fp,
                    idx + 1,
                )
                return True, data
            except httpx.HTTPStatusError as e:
                sanitized_e = _sanitize_http_error(e, self.api_keys)
                assert isinstance(sanitized_e, httpx.HTTPStatusError)
                is_insuff, is_per_request, _, resets_at, remaining = _classify_402_credit_info(
                    sanitized_e
                )
                if is_insuff and not is_per_request:
                    await _mark_key_cooldown(fp, resets_at=resets_at, remaining=remaining)
                    logger.warning(
                        "Firecrawl key fp=%s (slot=%d) returned 402 credit exhaustion (resets_at=%s); failing over",
                        fp,
                        idx + 1,
                        resets_at or "unknown",
                    )
                    last_402_error = sanitized_e
                    return False, None
                non_failover_err = sanitized_e
                return False, None
            except httpx.HTTPError as e:
                non_failover_err = _sanitize_http_error(e, self.api_keys)
                return False, None
            finally:
                key = None  # type: ignore[assignment]

        for idx in range(len(self.api_keys)):
            ok, data = await _try_slot(idx, self.api_keys[idx])
            if ok:
                return data  # type: ignore[return-value]
            if non_failover_err is not None:
                raise non_failover_err from None

        # Re-check once before returning budget_exceeded in case an earlier-cooled
        # key expired while another key's request was in flight.
        for idx in range(len(self.api_keys)):
            ok, data = await _try_slot(idx, self.api_keys[idx], recovered=True)
            if ok:
                return data  # type: ignore[return-value]
            if non_failover_err is not None:
                raise non_failover_err from None

        all_cooled, earliest_until = await get_firecrawl_pool_cooldown_state(self.api_keys)
        if not all_cooled:
            retried = False
            for idx in range(len(self.api_keys)):
                prev_tried = len(tried_fps)
                ok, data = await _try_slot(idx, self.api_keys[idx], recovered=True)
                if ok:
                    return data  # type: ignore[return-value]
                if non_failover_err is not None:
                    raise non_failover_err from None
                if len(tried_fps) > prev_tried:
                    retried = True
                    break
            if retried or last_402_error is None:
                all_cooled, earliest_until = await get_firecrawl_pool_cooldown_state(
                    self.api_keys
                )
        if all_cooled or last_402_error is None:
            raise self._build_all_cooled_402(method, url, earliest_until) from None
        raise last_402_error from None


class FirecrawlFetchAdapter:
    """FetchAdapter over Firecrawl /v2/scrape (one URL per call)."""

    name = "firecrawl"

    async def _fetch_one(
        self, api: FirecrawlAPI, url: str, req: FetchRequest, proxy: Optional[str]
    ) -> FetchResult:
        try:
            data = await api.scrape(url, proxy=proxy, max_age_seconds=req.max_age_seconds)
        except httpx.HTTPError as e:
            # DALTON-HOOK(firecrawl-budget): multi-key failover
            return FetchResult(
                url=url, error=_scrape_error(e, multi_key=len(api.api_keys) > 1)
            )

        if not data.get("success", False):
            # A 200 with success=false reports the outcome of scraping THIS url
            # — a dead domain, a page no engine could render — so it must not
            # count against the breaker every url shares. A real Firecrawl
            # outage arrives as 5xx or a transport error and is classified
            # above. Verified against the live API with an unresolvable host,
            # which returns 200 with SCRAPE_DNS_RESOLUTION_ERROR.
            code = data.get("code")
            message = str(data.get("error") or "Firecrawl returned success=false")
            return FetchResult(
                url=url,
                error=WebError(
                    type=WebErrorType.PROVIDER_ERROR,
                    message=clip_error(f"[{code}] {message}" if code else message),
                    provider_fault=False,
                    native_kind=str(code).lower() if code else None,
                ),
            )

        # Firecrawl returned a page, which it bills whatever the page is
        # (docs.firecrawl.dev/billing): a target's 403 or 404 and an empty
        # page fail here but are still charged.
        page = data.get("data") or {}
        metadata = page.get("metadata") or {}
        cost = _reported_credits(metadata)
        target_status = metadata.get("statusCode")
        if isinstance(target_status, int):
            err = _target_status_error(target_status)
            if err is not None:
                return FetchResult(url=url, error=err, billed=True, vendor_credits=cost)

        markdown = page.get("markdown") or ""
        if is_empty_markdown(markdown):
            return FetchResult(
                url=url, error=empty_content_error(), billed=True, vendor_credits=cost
            )
        return FetchResult(
            url=url,
            vendor_credits=cost,
            final_url=metadata.get("sourceURL") or url,
            title=metadata.get("title"),
            markdown=markdown,
        )

    async def fetch(self, req: FetchRequest, native_params: Dict[str, Any]) -> FetchResponse:
        try:
            api = FirecrawlAPI()
        except ValueError as e:
            return error_response(req.urls, missing_key_error(str(e)), self.name)
        proxy = native_params.get("proxy")
        results: List[FetchResult] = await asyncio.gather(
            *(self._fetch_one(api, u, req, proxy) for u in req.urls)
        )
        return FetchResponse(results=list(results), provider=self.name)


def _crawl_call_error(e: httpx.HTTPError, *, multi_key: Optional[bool] = None) -> WebToolError:
    """Wrap an httpx failure as a call-level WebToolError (crawl status overrides)."""
    if isinstance(e, httpx.HTTPStatusError):  # DALTON-HOOK(firecrawl-budget)
        credit_err = _firecrawl_402_error(e, multi_key=multi_key)
        if credit_err is not None:
            return WebToolError(credit_err)
    return WebToolError(error_from_httpx(e, status_overrides=_STATUS_OVERRIDES))


def _page_from_item(item: Dict[str, Any]) -> CrawlPage:
    """Normalize one crawl data[] item; per-page failures become CrawlPage.error."""
    metadata = item.get("metadata") or {}
    url = metadata.get("url") or metadata.get("sourceURL") or ""
    title = metadata.get("title")
    if isinstance(title, list):  # metadata.title may be string|array
        title = title[0] if title else None
    if isinstance(title, str):  # live pages embed stray whitespace/newlines
        title = title.strip() or None

    # Firecrawl bills every page a crawl returns, failed here or not.
    cost = _reported_credits(metadata)
    page_error = metadata.get("error")
    if page_error:
        return CrawlPage(
            url=url,
            title=title,
            error=WebError(type=WebErrorType.PROVIDER_ERROR, message=clip_error(str(page_error))),
            billed=True,
            vendor_credits=cost,
        )
    target_status = metadata.get("statusCode")
    if isinstance(target_status, int):
        err = _target_status_error(target_status)
        if err is not None:
            return CrawlPage(url=url, title=title, error=err, billed=True, vendor_credits=cost)

    markdown = item.get("markdown") or ""
    if is_empty_markdown(markdown):
        return CrawlPage(
            url=url, title=title, error=empty_content_error(), billed=True, vendor_credits=cost
        )
    return CrawlPage(url=url, title=title, markdown=markdown, vendor_credits=cost)


def _reported_credits(metadata: Dict[str, Any]) -> Optional[float]:
    """The page's ``creditsUsed``: one for a web page, one a page for a PDF
    (checked live 2026-11-16), whatever proxy fetched it. None when absent
    or not a non-negative number, which charges the page as one."""
    value = metadata.get("creditsUsed")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


_CRAWL_STATES = {
    "scraping": CrawlState.RUNNING,
    "completed": CrawlState.COMPLETED,
    "failed": CrawlState.FAILED,
    "cancelled": CrawlState.CANCELLED,
}


class FirecrawlCrawlAdapter:
    """CrawlAdapter over Firecrawl /v2/crawl (async job) + /v2/map."""

    name = "firecrawl"

    async def start_crawl(self, req: CrawlRequest, native_params: Dict[str, Any]) -> CrawlJob:
        try:
            api = FirecrawlAPI()
        except ValueError as e:
            raise WebToolError(missing_key_error(str(e)))
        payload: Dict[str, Any] = {
            "url": req.url,
            "limit": req.limit,
            "scrapeOptions": {"formats": [{"type": "markdown"}], "onlyMainContent": True},
        }
        if req.max_depth is not None:
            payload["maxDiscoveryDepth"] = req.max_depth
        if req.include_paths:
            payload["includePaths"] = req.include_paths
        if req.exclude_paths:
            payload["excludePaths"] = req.exclude_paths
        if req.query:
            # Natural-language steer; explicit params above override anything
            # the prompt would generate (Firecrawl semantics).
            payload["prompt"] = req.query
        payload.update(native_params)

        tool_err: Optional[WebToolError] = None
        try:
            data = await api.start_crawl(payload)
        except httpx.HTTPError as e:
            tool_err = _crawl_call_error(e, multi_key=len(api.api_keys) > 1)
        if tool_err is not None:
            api = None  # type: ignore[assignment]
            raise tool_err from None
        job_id = data.get("id")
        if not data.get("success", False) or not job_id:
            message = str(data.get("error") or "Firecrawl did not return a crawl id")
            raise WebToolError(WebError(type=WebErrorType.PROVIDER_ERROR, message=clip_error(message)))
        return CrawlJob(id=job_id, provider=self.name)

    async def crawl_status(self, job_id: str, skip: int = 0) -> CrawlStatus:
        """Poll job progress; returns pages past ``skip``, following pagination."""
        try:
            api = FirecrawlAPI()
        except ValueError as e:
            raise WebToolError(missing_key_error(str(e)))
        pages: List[CrawlPage] = []
        # DALTON-HOOK(firecrawl-budget): multi-key failover
        tool_err: Optional[WebToolError] = None
        try:
            data = await api.crawl_status(job_id, skip=skip)
            pages.extend(_page_from_item(item) for item in data.get("data") or [])
            # `next` is an absolute URL; present while more result pages exist.
            # Hop-bounded: a cursor that never clears must not pin the poll loop
            # past the crawl deadline (which is only checked between polls).
            next_url = data.get("next")
            hops = 0
            while next_url and hops < _MAX_STATUS_PAGES:
                chunk = await api.crawl_status(job_id, page_url=next_url)
                pages.extend(_page_from_item(item) for item in chunk.get("data") or [])
                next_url = chunk.get("next")
                hops += 1
            if next_url:
                logger.warning(
                    "Firecrawl crawl %s: status pagination truncated after %d pages",
                    job_id,
                    _MAX_STATUS_PAGES,
                )
        except httpx.HTTPError as e:
            tool_err = _crawl_call_error(e, multi_key=len(api.api_keys) > 1)
        if tool_err is not None:
            api = None  # type: ignore[assignment]
            raise tool_err from None

        state = _CRAWL_STATES.get(data.get("status"), CrawlState.RUNNING)
        error = None
        if state is CrawlState.FAILED:
            error = WebError(
                type=WebErrorType.PROVIDER_ERROR,
                message=str(data.get("error") or "Crawl job failed"),
            )
        return CrawlStatus(
            state=state,
            total=data.get("total") or 0,
            completed=data.get("completed") or 0,
            pages=pages,
            error=error,
        )

    async def cancel_crawl(self, job_id: str) -> None:
        """Best-effort cancel — a terminal/expired job 404s; never raises."""
        try:
            api = FirecrawlAPI()
            await api.cancel_crawl(job_id)
        except (ValueError, httpx.HTTPError) as e:
            logger.warning("Firecrawl crawl %s cancel failed: %s", job_id, e)

    async def map_site(self, req: MapRequest, native_params: Dict[str, Any]) -> List[UrlInfo]:
        try:
            api = FirecrawlAPI()
        except ValueError as e:
            raise WebToolError(missing_key_error(str(e)))
        payload: Dict[str, Any] = {"url": req.url, "limit": req.limit}
        if req.query:
            payload["search"] = req.query
        payload.update(native_params)

        tool_err: Optional[WebToolError] = None
        try:
            data = await api.map_site(payload)
        except httpx.HTTPError as e:
            # DALTON-HOOK(firecrawl-budget): multi-key failover
            tool_err = _crawl_call_error(e, multi_key=len(api.api_keys) > 1)
        if tool_err is not None:
            api = None  # type: ignore[assignment]
            raise tool_err from None
        if not data.get("success", True):
            message = str(data.get("error") or "Firecrawl map returned success=false")
            raise WebToolError(WebError(type=WebErrorType.PROVIDER_ERROR, message=clip_error(message)))

        links: List[UrlInfo] = []
        for link in data.get("links") or []:
            if isinstance(link, str):  # tolerate legacy bare-string links
                links.append(UrlInfo(url=link))
            elif isinstance(link, dict) and link.get("url"):
                links.append(
                    UrlInfo(
                        url=link["url"],
                        title=link.get("title"),
                        description=link.get("description"),
                    )
                )
        return links
