"""Standalone Core structured tool-calling probe for ``/api/v1/keys/test`` (Batch 5)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import time
from typing import Any

import httpx

from src.dalton.profile import is_core_profile
from src.server.services.llm.key_probe import sanitize_error

logger = logging.getLogger(__name__)

_TOOL_PROBE_TIMEOUT_S = 5.0
_TOOL_PROBE_REDIS_TTL_S = 30 * 24 * 3600
_TOOL_PROBE_REDIS_PREFIX = "dalton:tool_probe:"
_TOOL_PROBE_REDIS_HASH_PREFIX = "dalton:tool_probe:h:"
_PROBE_TOOL_NAME = "dalton_probe_ping"
_PROBE_CONCURRENCY = 2
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_MAX_RETRY_AFTER_S = 30.0
_PROBE_TOTAL_TIMEOUT_S = 45.0
_CORE_UNSUPPORTED_LIST_STATUSES: tuple[int, ...] = (400, 404, 405, 410, 422, 501)
_RESPONSES_FORMAT_MISMATCH_HINT = "网关只支持 Responses 接口，请在提供商设置里打开 Responses 格式"
_REDIS_UNVERIFIED_SENTINEL = "null"

# In-process fallback store when Redis is unavailable (e.g. unit tests or no-Redis setups).
# Maps user_id -> {provider: {"models": {model_id: bool | None}}} (per-model ONLY, never provider-wide).
_LOCAL_PROBE_STORE: dict[str, dict[str, dict[str, dict[str, bool | None]]]] = {}


class _ProbeDeadlineExceeded(Exception):
    """Raised internally when a probe wait or request exceeds the overall probe budget."""


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Forward requests to an owned transport while suppressing intermediate ``aclose()`` calls."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        return None

    async def aclose_owned(self) -> None:
        await self._inner.aclose()


@contextlib.asynccontextmanager
async def core_key_test_transport_scope(transport: Any):
    """Keep a per-request transport (e.g. platform ``HeldTransport``) open across
    ``/models``, key validation, and tool probing in Core, closing it once on exit.

    Strict no-op in Everflow profile (yields ``transport`` untouched).
    """
    if not is_core_profile() or transport is None:
        yield transport
        return
    borrowed = _BorrowedTransport(transport)
    try:
        yield borrowed
    finally:
        await borrowed.aclose_owned()


_OPENAI_CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": _PROBE_TOOL_NAME,
        "description": "Connectivity probe tool. Call with value 'ok'.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}

_OPENAI_RESPONSES_TOOL = {
    "type": "function",
    "name": _PROBE_TOOL_NAME,
    "description": "Connectivity probe tool. Call with value 'ok'.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
}

_ANTHROPIC_TOOL = {
    "name": _PROBE_TOOL_NAME,
    "description": "Connectivity probe tool. Call with value 'ok'.",
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
}

_GEMINI_TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": _PROBE_TOOL_NAME,
                "description": "Connectivity probe tool. Call with value 'ok'.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"value": {"type": "STRING"}},
                    "required": ["value"],
                },
            }
        ]
    }
]


def clear_tool_probe_cache(user_id: str | None = None) -> None:
    """Clear in-process tool-probe cache (test helper)."""
    if user_id is None:
        _LOCAL_PROBE_STORE.clear()
    else:
        _LOCAL_PROBE_STORE.pop(user_id, None)


async def _get_redis_client():
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if getattr(cache, "enabled", False) and getattr(cache, "client", None) is not None:
            return cache.client
    except Exception:
        pass
    return None


async def record_tool_calling_probe(
    user_id: str,
    provider: str,
    model_id: str | None,
    supported: bool | None,
) -> None:
    """Persist a tool-calling probe verdict strictly for ``(user_id, provider, model_id)``.

    Writes ``True``, ``False``, or ``None`` (unverified sentinel) atomically with TTL in
    one Redis transaction/pipeline so re-probing overwrites any stale ``True`` verdict.
    Falls back to ``_LOCAL_PROBE_STORE`` only when Redis is unavailable.
    """
    uid = (user_id or "").strip()
    prov = (provider or "").strip()
    mid = (model_id or "").strip()
    if not uid or not prov or not mid:
        return

    normalized: bool | None = supported if isinstance(supported, bool) else None
    client = await _get_redis_client()
    if client is not None:
        try:
            hash_key = f"{_TOOL_PROBE_REDIS_HASH_PREFIX}{uid}"
            field = json.dumps([prov, mid], separators=(",", ":"))
            encoded_val = (
                "1" if normalized is True else ("0" if normalized is False else _REDIS_UNVERIFIED_SENTINEL)
            )
            if hasattr(client, "pipeline"):
                async with client.pipeline(transaction=True) as pipe:
                    pipe.hset(hash_key, field, encoded_val)
                    pipe.expire(hash_key, _TOOL_PROBE_REDIS_TTL_S)
                    await pipe.execute()
            else:
                await client.hset(hash_key, field, encoded_val)
                if hasattr(client, "expire"):
                    await client.expire(hash_key, _TOOL_PROBE_REDIS_TTL_S)
            return
        except Exception as exc:
            logger.debug("Failed to persist tool probe result in Redis: %s", exc)

    user_bucket = _LOCAL_PROBE_STORE.setdefault(uid, {})
    prov_bucket = user_bucket.setdefault(prov, {"models": {}})
    prov_bucket["models"][mid] = normalized


def _decode_redis_text(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _parse_redis_verdict(raw: Any) -> tuple[bool, bool | None]:
    """Return ``(recognized, verdict)`` where verdict is ``True``, ``False``, or ``None`` (unverified)."""
    if isinstance(raw, bool):
        return True, raw
    text = _decode_redis_text(raw).strip().lower()
    if text in ("1", "true"):
        return True, True
    if text in ("0", "false"):
        return True, False
    if text in (_REDIS_UNVERIFIED_SENTINEL, "unverified", "none", ""):
        return True, None
    return False, None


async def get_recorded_tool_probes(user_id: str) -> dict[str, dict[str, dict[str, bool]]]:
    """Return per-model recorded boolean tool-calling probe results for ``user_id``.

    Redis is the source of truth whenever available so updates written by another
    worker are never shadowed by stale in-process state. ``_LOCAL_PROBE_STORE`` is
    consulted only when Redis is unavailable.
    """
    uid = (user_id or "").strip()
    if not uid:
        return {}

    seen_models: dict[tuple[str, str], bool | None] = {}
    read_from_redis = False
    client = await _get_redis_client()
    if client is not None:
        try:
            if hasattr(client, "hgetall"):
                raw_hash = await client.hgetall(f"{_TOOL_PROBE_REDIS_HASH_PREFIX}{uid}")
                if isinstance(raw_hash, dict):
                    for raw_field, raw_val in raw_hash.items():
                        ok, val = _parse_redis_verdict(raw_val)
                        if not ok:
                            continue
                        try:
                            pair = json.loads(_decode_redis_text(raw_field))
                        except ValueError:
                            continue
                        if (
                            isinstance(pair, list)
                            and len(pair) == 2
                            and isinstance(pair[0], str)
                            and isinstance(pair[1], str)
                            and pair[0]
                            and pair[1]
                        ):
                            seen_models[(pair[0], pair[1])] = val
            if hasattr(client, "get"):
                raw = await client.get(f"{_TOOL_PROBE_REDIS_PREFIX}{uid}")
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        for prov, entry in parsed.items():
                            if not isinstance(entry, dict):
                                continue
                            models_map = entry.get("models")
                            if isinstance(models_map, dict) and models_map:
                                for mid, val in models_map.items():
                                    if isinstance(val, bool):
                                        seen_models.setdefault((str(prov), str(mid)), val)
            read_from_redis = True
        except Exception as exc:
            logger.debug("Failed to read tool probe results from Redis: %s", exc)

    if not read_from_redis:
        local = _LOCAL_PROBE_STORE.get(uid)
        if isinstance(local, dict):
            for prov, entry in local.items():
                if isinstance(entry, dict):
                    models_map = entry.get("models")
                    if isinstance(models_map, dict):
                        for k, v in models_map.items():
                            if isinstance(v, bool) or v is None:
                                seen_models[(str(prov), str(k))] = v

    merged: dict[str, dict[str, dict[str, bool]]] = {}
    for (prov, mid), val in seen_models.items():
        if isinstance(val, bool):
            merged.setdefault(prov, {"models": {}})["models"][mid] = val
    return merged


async def attach_tool_probe_results(
    user_id: str,
    keys_response: dict[str, Any],
) -> dict[str, Any]:
    """Attach per-model Core tool-calling probe verdicts to ``GET /api/v1/users/me/api-keys``.

    Only emits ``tool_calling_models`` (never a provider-wide verdict so unprobed
    models on the same provider remain unverified).
    In Everflow profile this is a strict no-op returning ``keys_response`` unchanged.
    """
    if not is_core_profile() or not isinstance(keys_response, dict):
        return keys_response
    providers = keys_response.get("providers")
    if not isinstance(providers, list) or not providers:
        return keys_response
    recorded = await get_recorded_tool_probes(user_id)
    if not recorded:
        return keys_response

    updated_rows: list[dict[str, Any]] = []
    changed = False
    for row in providers:
        if not isinstance(row, dict):
            updated_rows.append(row)
            continue
        prov = str(row.get("provider") or "")
        entry = recorded.get(prov)
        models_map = entry.get("models") if isinstance(entry, dict) else None
        if not isinstance(models_map, dict) or not models_map:
            updated_rows.append(row)
            continue
        new_row = dict(row)
        new_row["tool_calling_models"] = {
            str(k): bool(v) for k, v in models_map.items() if isinstance(v, bool)
        }
        changed = True
        updated_rows.append(new_row)

    if not changed:
        return keys_response
    return {**keys_response, "providers": updated_rows}


def _has_openai_chat_tool_call(data: dict[str, Any]) -> bool:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return False
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and len(tool_calls) > 0:
        return True
    if isinstance(msg.get("function_call"), dict):
        return True
    return False


def _is_valid_responses_payload(body: Any) -> bool:
    """Return True only when ``body`` is a genuine OpenAI Responses JSON object."""
    return isinstance(body, dict) and (
        isinstance(body.get("output"), list) or body.get("object") == "response"
    )


def _has_openai_responses_tool_call(data: dict[str, Any]) -> bool:
    output = data.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in output
    )


def _has_anthropic_tool_call(data: dict[str, Any]) -> bool:
    content = data.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _has_gemini_tool_call(data: dict[str, Any]) -> bool:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return False
    return any(isinstance(p, dict) and "functionCall" in p for p in parts)


def select_core_key_probe_model(
    explicit_model: str | None,
    explicit_models: list[str] | None = None,
) -> str | None:
    """Return the caller-selected model id for Core ``key_probe.check_key`` fallback.

    Returns ``None`` in Everflow profile so Everflow key probing is untouched.
    """
    if not is_core_profile():
        return None
    if isinstance(explicit_model, str) and explicit_model.strip():
        return explicit_model.strip()
    if isinstance(explicit_models, list):
        for item in explicit_models:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def core_key_probe_options(
    explicit_model: str | None,
    explicit_models: list[str] | None = None,
    provider_info: dict[str, Any] | None = None,
    custom_provider_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return Core-only kwargs for ``key_probe.check_key``.

    Returns ``{}`` in Everflow profile so Everflow key probing is untouched.
    """
    if not is_core_profile():
        return {}
    if custom_provider_entry is not None:
        use_response_api = bool(custom_provider_entry.get("use_response_api"))
    else:
        use_response_api = bool((provider_info or {}).get("use_response_api"))
    return {
        "probe_model": select_core_key_probe_model(explicit_model, explicit_models),
        "absent_list_statuses": _CORE_UNSUPPORTED_LIST_STATUSES,
        "use_response_api": use_response_api,
    }


def _select_probe_models(
    provider: str,
    provider_info: dict[str, Any],
    check_result: dict[str, Any],
    explicit_model: str | None,
    explicit_models: list[str] | None,
) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if not isinstance(raw, str):
            return
        mid = raw.strip()
        if mid and mid not in seen:
            seen.add(mid)
            targets.append(mid)

    _add(explicit_model)
    if isinstance(explicit_models, list):
        for item in explicit_models:
            _add(item)
    if targets:
        return targets

    from_check = check_result.get("model")
    if isinstance(from_check, str) and from_check.strip():
        _add(from_check)
        return targets

    try:
        from src.llms.llm import LLM as LLMFactory

        mc = LLMFactory.get_model_config()
        manifest_models = mc.manifest.get("models") or {}
        candidates = manifest_models.get(provider) or []
        if not candidates:
            parent = provider_info.get("parent_provider")
            if parent:
                candidates = manifest_models.get(parent) or []
        for item in candidates:
            if isinstance(item, dict) and item.get("id"):
                _add(str(item["id"]))
                break
    except Exception:
        pass
    return targets


def _redact_probe_error(msg: str, api_key: str) -> str:
    cleaned = msg.replace(api_key, "[REDACTED]") if api_key else msg
    return sanitize_error(cleaned)


def _parse_retry_after_s(resp: httpx.Response, attempt: int) -> float:
    """Parse HTTP Retry-After header (seconds or HTTP-date), else short backoff."""
    raw = (resp.headers.get("retry-after") or "").strip()
    if raw:
        try:
            seconds = float(raw)
            return max(0.0, min(seconds, _RATE_LIMIT_MAX_RETRY_AFTER_S))
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, min(delta, _RATE_LIMIT_MAX_RETRY_AFTER_S))
        except Exception:
            pass
    return 0.25 * (attempt + 1)


class _AdaptiveProbeLimiter:
    """Concurrency gate that starts at ``initial`` and steps down to 1 on HTTP 429."""

    def __init__(self, initial: int = _PROBE_CONCURRENCY) -> None:
        self._limit = max(1, initial)
        self._in_flight = 0
        self._cond = asyncio.Condition()

    async def acquire(self, deadline: float | None = None) -> None:
        async with self._cond:
            if deadline is None:
                await self._cond.wait_for(lambda: self._in_flight < self._limit)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ProbeDeadlineExceeded("probe total timeout exceeded")
                try:
                    await asyncio.wait_for(
                        self._cond.wait_for(lambda: self._in_flight < self._limit),
                        timeout=remaining,
                    )
                except TimeoutError as exc:
                    raise _ProbeDeadlineExceeded("probe total timeout exceeded") from exc
            self._in_flight += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    async def on_rate_limit(self) -> None:
        async with self._cond:
            if self._limit > 1:
                self._limit = 1
                self._cond.notify_all()


async def _post_with_rate_limit_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json_payload: dict[str, Any],
    limiter: _AdaptiveProbeLimiter | None = None,
    deadline: float | None = None,
    max_retries: int = _RATE_LIMIT_MAX_RETRIES,
) -> httpx.Response:
    """POST with bounded retries on HTTP 429 honoring Retry-After up to 30s within total deadline.

    When a shared ``limiter`` is provided, each HTTP attempt acquires a slot,
    releases it while sleeping ``Retry-After``, and steps concurrency down to 1
    on the first 429 so concurrency-limited gateways never starve retries.
    """
    for attempt in range(max_retries + 1):
        if limiter is not None:
            await limiter.acquire(deadline=deadline)
        try:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ProbeDeadlineExceeded("probe total timeout exceeded")
                try:
                    resp = await asyncio.wait_for(
                        client.post(url, headers=headers, json=json_payload),
                        timeout=remaining,
                    )
                except TimeoutError as exc:
                    raise _ProbeDeadlineExceeded("probe total timeout exceeded") from exc
            else:
                resp = await client.post(url, headers=headers, json=json_payload)
        finally:
            if limiter is not None:
                await limiter.release()
        if resp.status_code != 429 or attempt >= max_retries:
            return resp
        if limiter is not None:
            await limiter.on_rate_limit()
        delay = _parse_retry_after_s(resp, attempt)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or delay > remaining:
                raise _ProbeDeadlineExceeded(
                    f"rate limited (HTTP 429); Retry-After {delay:.1f}s exceeds total probe timeout"
                )
        if delay > 0:
            await asyncio.sleep(delay)
    return resp


async def _run_structured_tool_probe(
    *,
    sdk: str,
    api_key: str,
    model_id: str,
    provider_info: dict[str, Any],
    base_url: str | None,
    use_response_api: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
    limiter: _AdaptiveProbeLimiter | None = None,
    deadline: float | None = None,
    timeout: float = _TOOL_PROBE_TIMEOUT_S,
) -> tuple[bool | None, str | None]:
    """Execute one lightweight structured tool-calling request.

    Honors ``use_response_api`` strictly as passed (never inheriting parent's
    ``use_response_api`` onto a Chat Completions custom gateway). If a Chat
    Completions gateway returns 404/405/501 and falls back to ``/responses``:
    - HTTP 200 on ``/responses`` reports unverified with a format-mismatch hint
      so the user enables Responses API on the saved provider first.
    - HTTP 400/422 on ``/responses`` fallback reports unverified (never unsupported).
    """
    eff_base = (base_url or provider_info.get("base_url") or "").strip().rstrip("/")
    sdk_norm = (sdk or "openai").strip().lower()
    fell_back_to_responses = False

    try:
        client_cm = (
            contextlib.nullcontext(client)
            if client is not None
            else httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        )
        async with client_cm as http:
            if sdk_norm == "anthropic":
                url = f"{eff_base or 'https://api.anthropic.com'}/v1/messages"
                headers: dict[str, str] = {
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                if api_key:
                    headers["x-api-key"] = api_key
                payload = {
                    "model": model_id,
                    "max_tokens": 64,
                    "messages": [
                        {"role": "user", "content": f"Call {_PROBE_TOOL_NAME} with value='ok'."}
                    ],
                    "tools": [_ANTHROPIC_TOOL],
                    "tool_choice": {"type": "tool", "name": _PROBE_TOOL_NAME},
                }
                resp = await _post_with_rate_limit_retry(
                    http, url, headers=headers, json_payload=payload, limiter=limiter, deadline=deadline
                )
                checker = _has_anthropic_tool_call
            elif sdk_norm == "gemini":
                root = eff_base or "https://generativelanguage.googleapis.com/v1beta"
                url = f"{root}/models/{model_id}:generateContent"
                headers = {"content-type": "application/json"}
                if api_key:
                    headers["x-goog-api-key"] = api_key
                payload = {
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": f"Call {_PROBE_TOOL_NAME} with value='ok'."}],
                        }
                    ],
                    "tools": _GEMINI_TOOLS,
                    "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                }
                resp = await _post_with_rate_limit_retry(
                    http, url, headers=headers, json_payload=payload, limiter=limiter, deadline=deadline
                )
                checker = _has_gemini_tool_call
            elif sdk_norm not in ("claude_code", "codex", "unknown", ""):
                root = eff_base or "https://api.openai.com/v1"
                headers = {"content-type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                responses_url = f"{root}/responses"
                responses_payload = {
                    "model": model_id,
                    "input": f"Call {_PROBE_TOOL_NAME} with value='ok'.",
                    "tools": [_OPENAI_RESPONSES_TOOL],
                    "tool_choice": "required",
                    "max_output_tokens": 64,
                }
                if use_response_api:
                    resp = await _post_with_rate_limit_retry(
                        http,
                        responses_url,
                        headers=headers,
                        json_payload=responses_payload,
                        limiter=limiter,
                        deadline=deadline,
                    )
                    checker = _has_openai_responses_tool_call
                else:
                    url = f"{root}/chat/completions"
                    payload = {
                        "model": model_id,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Call {_PROBE_TOOL_NAME} with value='ok'.",
                            }
                        ],
                        "tools": [_OPENAI_CHAT_TOOL],
                        "tool_choice": {
                            "type": "function",
                            "function": {"name": _PROBE_TOOL_NAME},
                        },
                        "max_tokens": 64,
                    }
                    resp = await _post_with_rate_limit_retry(
                        http, url, headers=headers, json_payload=payload, limiter=limiter, deadline=deadline
                    )
                    if resp.status_code in (404, 405, 501):
                        # Responses-only gateway while saved format is Chat Completions:
                        # check /responses to distinguish format mismatch from unsupported endpoint.
                        fell_back_to_responses = True
                        resp = await _post_with_rate_limit_retry(
                            http,
                            responses_url,
                            headers=headers,
                            json_payload=responses_payload,
                            limiter=limiter,
                            deadline=deadline,
                        )
                        checker = _has_openai_responses_tool_call
                    else:
                        if resp.status_code in (400, 422):
                            # Some OpenAI-compatible gateways disallow named tool_choice
                            # while supporting tools with tool_choice='auto'.
                            fallback_payload = dict(payload)
                            fallback_payload["tool_choice"] = "auto"
                            resp = await _post_with_rate_limit_retry(
                                http,
                                url,
                                headers=headers,
                                json_payload=fallback_payload,
                                limiter=limiter,
                                deadline=deadline,
                            )
                        checker = _has_openai_chat_tool_call
            else:
                return None, f"unsupported sdk for tool probe: {sdk_norm}"

        if fell_back_to_responses:
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return None, "non-JSON response on /responses fallback (unverified)"
                if _is_valid_responses_payload(body):
                    return None, _RESPONSES_FORMAT_MISMATCH_HINT
                return None, "unexpected non-Responses JSON payload on /responses fallback (unverified)"
            if resp.status_code == 429:
                return None, _redact_probe_error("rate limited (HTTP 429) after retries", api_key)
            if resp.status_code in (400, 422):
                detail = _redact_probe_error(resp.text[:240], api_key)
                return None, f"HTTP {resp.status_code} on /responses fallback (unverified): {detail}"
            return None, _redact_probe_error(f"HTTP {resp.status_code}", api_key)

        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                return None, "invalid JSON from tool probe"
            if isinstance(body, dict) and checker(body):
                return True, None
            return False, "model did not emit a structured tool call"

        if resp.status_code == 429:
            return None, _redact_probe_error("rate limited (HTTP 429) after retries", api_key)

        if resp.status_code in (400, 422):
            detail = _redact_probe_error(resp.text[:240], api_key)
            return False, f"HTTP {resp.status_code}: {detail}"

        return None, _redact_probe_error(f"HTTP {resp.status_code}", api_key)
    except _ProbeDeadlineExceeded as exc:
        return None, _redact_probe_error(str(exc), api_key)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return None, _redact_probe_error(str(exc), api_key)
    except Exception as exc:
        return None, _redact_probe_error(str(exc), api_key)


async def probe_tool_calling_for_key_test(
    *,
    user_id: str,
    provider: str,
    provider_info: dict[str, Any],
    api_key: str,
    base_url: str | None,
    check_result: dict[str, Any],
    model: str | None = None,
    models: list[str] | None = None,
    custom_provider_entry: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = _TOOL_PROBE_TIMEOUT_S,
    total_timeout: float = _PROBE_TOTAL_TIMEOUT_S,
) -> dict[str, Any]:
    """In Standalone Core profile, run structured tool-calling probes after ``check_key`` succeeds.

    Records verdicts per model atomically as each probe finishes (never provider-wide),
    uses one shared client across concurrent probes so shared transports are closed only
    after all probes finish, honors ``Retry-After`` within ``total_timeout``, and refuses
    to mark a Chat Completions provider confirmed when the gateway only serves Responses.
    Everflow profile returns ``check_result`` verbatim without making any extra call.
    """
    if not is_core_profile():
        return check_result
    if not isinstance(check_result, dict) or not check_result.get("success"):
        return check_result

    probe_models = _select_probe_models(provider, provider_info, check_result, model, models)
    if not probe_models:
        return {**check_result, "tool_calling": None}

    sdk = str(provider_info.get("sdk") or "openai")
    # Custom providers explicitly declare whether they speak Responses API; when
    # absent/False, they use Chat Completions and must NOT inherit parent_provider's
    # use_response_api=True (e.g. parent_provider='openai').
    if custom_provider_entry is not None:
        configured_use_response_api = bool(custom_provider_entry.get("use_response_api"))
    else:
        configured_use_response_api = bool(provider_info.get("use_response_api"))

    # If key check succeeded only by falling back from /chat/completions to /responses
    # while the saved provider format is Chat Completions, do NOT confirm tool calling:
    # runtime turns would still hit /chat/completions and fail. Overwrite any stale
    # prior True verdict as unverified and return the format hint.
    if check_result.get("used_response_api") and not configured_use_response_api:
        outcomes = [(mid, None, _RESPONSES_FORMAT_MISMATCH_HINT) for mid in probe_models]
        for mid in probe_models:
            await record_tool_calling_probe(user_id, provider, mid, None)
    else:
        limiter = _AdaptiveProbeLimiter(_PROBE_CONCURRENCY)
        deadline = time.monotonic() + max(0.0, total_timeout)

        async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as shared_client:
            async def _probe_one(mid: str) -> tuple[str, bool | None, str | None]:
                supported, err = await _run_structured_tool_probe(
                    sdk=sdk,
                    api_key=api_key,
                    model_id=mid,
                    provider_info=provider_info,
                    base_url=base_url,
                    use_response_api=configured_use_response_api,
                    client=shared_client,
                    limiter=limiter,
                    deadline=deadline,
                    timeout=timeout,
                )
                await record_tool_calling_probe(user_id, provider, mid, supported)
                return mid, supported, err

            outcomes = await asyncio.gather(*(_probe_one(mid) for mid in probe_models))

    results_by_model: dict[str, bool | None] = {}
    errors_by_model: dict[str, str] = {}
    details_by_model: dict[str, dict[str, Any]] = {}
    first_err: str | None = None
    format_hint: str | None = None
    for mid, supported, err in outcomes:
        results_by_model[mid] = supported
        if err == _RESPONSES_FORMAT_MISMATCH_HINT:
            format_hint = _RESPONSES_FORMAT_MISMATCH_HINT
        if supported is True:
            details_by_model[mid] = {"status": "supported", "tool_calling": True}
        elif supported is False:
            reason = err or "tool calling unsupported"
            errors_by_model[mid] = reason
            details_by_model[mid] = {"status": "unsupported", "tool_calling": False, "reason": reason}
            if first_err is None:
                first_err = reason
        else:
            reason = err or "tool calling unverified"
            errors_by_model[mid] = reason
            details_by_model[mid] = {"status": "unverified", "tool_calling": None, "reason": reason}
            if first_err is None:
                first_err = reason

    primary_model = probe_models[0]
    primary_supported = results_by_model.get(primary_model)
    out = dict(check_result)
    out["tool_calling"] = primary_supported
    out["tool_calling_model"] = primary_model
    out["tool_calling_models"] = results_by_model
    out["tool_calling_details"] = details_by_model
    if errors_by_model:
        out["tool_calling_errors"] = errors_by_model
    if first_err and primary_supported is not True:
        out["tool_calling_error"] = first_err
    if format_hint:
        out["tool_calling_hint"] = format_hint
    return out
