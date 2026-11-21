"""Tests for Firecrawl multi-key primary/backup failover, Redis cooldown, and crawl pinning."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import create_autospec

import httpx
import pytest

import src.tools.web.breaker as breaker_mod
import src.tools.web.fetch as fetch_mod
import src.tools.web.providers._shared as shared_mod
import src.tools.web.providers.firecrawl as fc_mod
import src.utils.cache.redis_cache as redis_cache_mod
from src.dalton.settings import get_dalton_config
from src.tools.web.breaker import CircuitState
from src.tools.web.providers.inhouse import InhouseFetchAdapter
from src.tools.web.router import FetchRouter
from src.tools.web.types import (
    CrawlJob,
    CrawlRequest,
    CrawlState,
    FetchRequest,
    FetchResponse,
    FetchResult,
    MapRequest,
    WebError,
    WebErrorType,
    WebToolError,
)
from src.utils.cache.redis_cache import RedisCacheClient

SECRET_KEY_1 = "fc-secret-primary-alpha-111111"
SECRET_KEY_2 = "fc-secret-backup-beta-222222"
SECRET_KEY_3 = "fc-secret-backup-gamma-333333"


@pytest.fixture(autouse=True)
def _clean_firecrawl_env_and_state(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_KEY_2",
        "FIRECRAWL_API_KEY_3",
        "FIRECRAWL_API_KEY_4",
    ):
        monkeypatch.delenv(name, raising=False)
    fc_mod._INMEM_COOLDOWNS.clear()
    fc_mod._INMEM_CRAWL_KEYS.clear()
    get_dalton_config.cache_clear()
    yield
    fc_mod._INMEM_COOLDOWNS.clear()
    fc_mod._INMEM_CRAWL_KEYS.clear()
    get_dalton_config.cache_clear()


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)

    def _make_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(shared_mod.httpx, "AsyncClient", _make_client)


def _make_fake_redis_cache(
    *,
    fail_reads: bool = False,
    fail_writes: bool = False,
) -> tuple[RedisCacheClient, Dict[str, tuple[Any, Optional[int]]]]:
    store: Dict[str, tuple[Any, Optional[int]]] = {}
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = True

    class _FakeRedisClient:
        async def eval(
            self,
            script: str,
            numkeys: int,
            key: str,
            serialized: str,
            ttl: int,
            until_repr: str,
        ) -> Optional[bytes]:
            if fail_writes:
                raise RuntimeError("redis write down")
            existing = store.get(key)
            if existing is not None:
                kept = existing[0]
                if (
                    isinstance(kept, dict)
                    and float(kept.get("cooldown_until", 0) or 0) >= float(until_repr)
                ):
                    return json.dumps(kept).encode("utf-8")
            store[key] = (json.loads(serialized), int(ttl))
            return None

    cache.client = _FakeRedisClient()

    async def _get_strict(key: str) -> Any:
        if fail_reads:
            raise RuntimeError("redis read down")
        item = store.get(key)
        return item[0] if item is not None else None

    async def _get(key: str) -> Any:
        if fail_reads:
            return None
        item = store.get(key)
        return item[0] if item is not None else None

    async def _set(key: str, value: Any, ttl: Optional[int] = None) -> bool:
        if fail_writes:
            raise RuntimeError("redis write down")
        store[key] = (value, ttl)
        return True

    cache.get_strict.side_effect = _get_strict
    cache.get.side_effect = _get
    cache.set.side_effect = _set
    return cache, store


def _bearer_key(request: httpx.Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth.removeprefix("Bearer ").strip()


def _402_payload(
    resets_at: Optional[str] = None,
    *,
    remaining_credits: Optional[int] = 0,
    error: str = "Insufficient credits",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "success": False,
        "code": "insufficient_credits",
        "error": error,
    }
    if remaining_credits is not None:
        body["remaining_credits"] = remaining_credits
    if resets_at is not None:
        body["resets_at"] = resets_at
    return body


def _assert_no_raw_keys_in_logs(caplog: pytest.LogCaptureFixture, keys: List[str]) -> None:
    for record in caplog.records:
        msg = record.getMessage()
        for raw_key in keys:
            assert raw_key not in msg, f"Raw API key leaked in log: {msg}"


def _assert_no_raw_keys_in_exception_chain(exc: BaseException, keys: List[str]) -> None:
    seen_ids: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen_ids:
        seen_ids.add(id(cur))
        text_blobs = [str(cur), repr(cur)]
        req = getattr(cur, "request", None)
        if req is not None:
            text_blobs.append(str(dict(req.headers)))
        resp = getattr(cur, "response", None)
        if resp is not None:
            text_blobs.append(resp.text)
            if getattr(resp, "request", None) is not None:
                text_blobs.append(str(dict(resp.request.headers)))
            for hist in getattr(resp, "history", ()):
                if getattr(hist, "request", None) is not None:
                    text_blobs.append(str(dict(hist.request.headers)))
        for blob in text_blobs:
            for raw_key in keys:
                assert raw_key not in blob, f"Raw API key leaked in exception chain: {blob}"
        cur = cur.__cause__ or cur.__context__


class TestKeyLoading:
    def test_loads_sequential_keys_and_stops_at_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        # Gap at _3 -> _4 must NOT be loaded
        monkeypatch.setenv("FIRECRAWL_API_KEY_4", SECRET_KEY_3)

        assert fc_mod.load_firecrawl_api_keys() == [SECRET_KEY_1, SECRET_KEY_2]

    def test_empty_first_key_stops_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        assert fc_mod.load_firecrawl_api_keys() == []


@pytest.mark.asyncio
class TestExceptionSanitization:
    async def test_two_keys_402_exception_and_chain_contain_no_raw_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json=_402_payload("2026-11-26T00:00:00Z"))

        _install_mock_transport(monkeypatch, handler)

        with pytest.raises(httpx.HTTPStatusError) as api_exc:
            await fc_mod.FirecrawlAPI().scrape("https://site.example/doc")
        _assert_no_raw_keys_in_exception_chain(api_exc.value, [SECRET_KEY_1, SECRET_KEY_2])
        assert api_exc.value.__cause__ is None
        assert api_exc.value.__context__ is None
        assert api_exc.value.request.headers.get("authorization") in (None, "[REDACTED]")

        fc_mod._INMEM_COOLDOWNS.clear()
        with pytest.raises(WebToolError) as crawl_exc:
            await fc_mod.FirecrawlCrawlAdapter().start_crawl(
                CrawlRequest(url="https://site.example", limit=5), {}
            )
        _assert_no_raw_keys_in_exception_chain(crawl_exc.value, [SECRET_KEY_1, SECRET_KEY_2])

    async def test_read_timeout_exception_and_traceback_contain_no_raw_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        query_secret = "fc-query-secret-999"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(
                f"timed out with {SECRET_KEY_1} on {request.url}",
                request=request,
            )

        _install_mock_transport(monkeypatch, handler)

        with pytest.raises(httpx.ReadTimeout) as api_exc:
            await fc_mod.FirecrawlAPI().crawl_status(
                "job-rt-1",
                page_url=f"https://api.firecrawl.dev/v2/crawl/job-rt-1?token={query_secret}",
            )
        assert api_exc.value.__cause__ is None
        assert api_exc.value.__context__ is None
        assert "ReadTimeout" in str(api_exc.value)
        assert "GET" in str(api_exc.value)
        assert "https://api.firecrawl.dev/v2/crawl/job-rt-1" in str(api_exc.value)
        assert query_secret not in str(api_exc.value)
        assert query_secret not in str(api_exc.value.request.url)
        _assert_no_raw_keys_in_exception_chain(
            api_exc.value, [SECRET_KEY_1, SECRET_KEY_2, query_secret]
        )
        api_tb = "".join(
            traceback.format_exception(
                type(api_exc.value), api_exc.value, api_exc.value.__traceback__
            )
        )
        for secret in (SECRET_KEY_1, SECRET_KEY_2, query_secret):
            assert secret not in api_tb

        with pytest.raises(WebToolError) as crawl_exc:
            await fc_mod.FirecrawlCrawlAdapter().start_crawl(
                CrawlRequest(url="https://site.example", limit=5), {}
            )
        assert crawl_exc.value.__cause__ is None
        assert crawl_exc.value.__context__ is None
        _assert_no_raw_keys_in_exception_chain(crawl_exc.value, [SECRET_KEY_1, SECRET_KEY_2])
        crawl_tb = "".join(
            traceback.format_exception(
                type(crawl_exc.value), crawl_exc.value, crawl_exc.value.__traceback__
            )
        )
        assert SECRET_KEY_1 not in crawl_tb
        assert SECRET_KEY_2 not in crawl_tb

    async def test_web_fetch_deadline_timeout_exception_and_logs_contain_no_raw_keys(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)

        class _HangingAsyncClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_HangingAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        monkeypatch.setattr(shared_mod.httpx, "AsyncClient", _HangingAsyncClient)
        svc = fetch_mod.FetchService(chain=["firecrawl"])
        monkeypatch.setattr(fetch_mod, "get_fetch_service", lambda: svc)

        async def _no_cache() -> None:
            return None

        monkeypatch.setattr(fetch_mod, "_get_cache_client", _no_cache)
        monkeypatch.setattr(fetch_mod, "WEB_FETCH_DEADLINE", 0.05)

        with caplog.at_level(logging.DEBUG, logger="src.tools.web"):
            content, artifact = await fetch_mod.web_fetch(
                "https://site.example/hang",
                "summarize",
                model="unused",
                use_cache=False,
                include_sitemap=False,
            )

        assert artifact["error"] == "timeout"
        assert SECRET_KEY_1 not in content and SECRET_KEY_2 not in content
        assert SECRET_KEY_1 not in str(artifact) and SECRET_KEY_2 not in str(artifact)
        _assert_no_raw_keys_in_logs(caplog, [SECRET_KEY_1, SECRET_KEY_2])

        # Also inspect the exact TimeoutError raised when the WebFetch deadline cancels
        # the in-flight Firecrawl request (traceback.format_exception, str(), repr()).
        with pytest.raises(TimeoutError) as deadline_exc:
            async with asyncio.timeout(0.05):
                await fetch_mod._fetch_and_extract(
                    "https://site.example/hang",
                    "summarize",
                    "unused",
                    False,
                    False,
                    {"type": "web_fetch", "url": "https://site.example/hang"},
                    [],
                )

        tb_text = "".join(
            traceback.format_exception(
                type(deadline_exc.value),
                deadline_exc.value,
                deadline_exc.value.__traceback__,
            )
        )
        for secret in (SECRET_KEY_1, SECRET_KEY_2):
            assert secret not in tb_text
            assert secret not in str(deadline_exc.value)
            assert secret not in repr(deadline_exc.value)
        _assert_no_raw_keys_in_exception_chain(
            deadline_exc.value, [SECRET_KEY_1, SECRET_KEY_2]
        )


@pytest.mark.asyncio
class TestMultiKeyFailoverAndCooldown:
    async def test_first_key_402_retries_second_key_and_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        resets_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload(resets_at))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "# Recovered\n\nFetched via backup key.",
                        "metadata": {"sourceURL": "https://site.example/doc", "statusCode": 200},
                    },
                },
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        with caplog.at_level(logging.DEBUG, logger="src.tools.web.providers.firecrawl"):
            resp1 = await adapter.fetch(FetchRequest(urls=["https://site.example/doc"]), {})
            # Subsequent request while Key 1 is cooling must go straight to Key 2 (no retry on Key 1)
            resp2 = await adapter.fetch(FetchRequest(urls=["https://site.example/doc2"]), {})

        assert resp1.results[0].ok
        assert resp2.results[0].ok
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2]
        _assert_no_raw_keys_in_logs(caplog, [SECRET_KEY_1, SECRET_KEY_2])

    async def test_positive_remaining_credits_402_does_not_failover_cooldown_or_trip_breaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        seen_keys: List[str] = []
        oversized = {"on": True}

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if oversized["on"]:
                return httpx.Response(
                    402,
                    json=_402_payload(
                        "2026-11-26T00:00:00Z",
                        remaining_credits=25,
                        error="Insufficient credits for this request",
                    ),
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "# Small page\n\nFits in 25 credits.",
                        "metadata": {"sourceURL": "https://site.example/small", "statusCode": 200},
                    },
                },
            )

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")

        # Repeated per-request credit overages (> 0 remaining) must NOT failover,
        # must NOT cool down Key 1, and must NOT trip the shared breaker.
        for i in range(6):
            r = await router.fetch(FetchRequest(urls=[f"https://site.example/big/{i}"]))
            assert r.results[0].error is not None
            assert r.results[0].error.type == WebErrorType.PROVIDER_ERROR
            assert r.results[0].error.message == "本次请求所需额度超过账户余额"
            assert r.results[0].error.provider_fault is False
            assert r.results[0].error.native_kind == "insufficient_credits_for_request"

        assert seen_keys == [SECRET_KEY_1] * 6
        assert fc_mod._INMEM_COOLDOWNS == {}
        assert store == {}
        assert not fc_entry.breaker.is_open()
        assert "firecrawl" not in router._budget_errors

        # Next smaller request still succeeds on Key 1 immediately
        oversized["on"] = False
        ok = await router.fetch(FetchRequest(urls=["https://site.example/small"]))
        assert ok.results[0].ok
        assert seen_keys[-1] == SECRET_KEY_1

    async def test_map_site_uses_same_failover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload())
            return httpx.Response(
                200,
                json={"success": True, "links": [{"url": "https://site.example/a", "title": "A"}]},
            )

        _install_mock_transport(monkeypatch, handler)
        links = await fc_mod.FirecrawlCrawlAdapter().map_site(
            MapRequest(url="https://site.example", limit=10), {}
        )

        assert len(links) == 1 and links[0].url == "https://site.example/a"
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2]

    async def test_missing_resets_at_uses_default_24h_cooldown_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert fc_mod._DEFAULT_COOLDOWN_SECONDS == 24 * 60 * 60
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        def handler(request: httpx.Request) -> httpx.Response:
            if _bearer_key(request) == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload(resets_at=None))
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok markdown body", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        before = time.time()
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(FetchRequest(urls=["https://site.example/x"]), {})
        after = time.time()

        assert resp.results[0].ok
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        redis_key = f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"
        assert redis_key in store
        payload, ttl = store[redis_key]
        assert ttl is not None and fc_mod._DEFAULT_COOLDOWN_SECONDS - 5 <= ttl <= fc_mod._DEFAULT_COOLDOWN_SECONDS + 1
        assert before + fc_mod._DEFAULT_COOLDOWN_SECONDS - 1 <= payload["cooldown_until"] <= after + fc_mod._DEFAULT_COOLDOWN_SECONDS + 1
        assert SECRET_KEY_1 not in redis_key
        assert SECRET_KEY_1 not in str(payload)

    async def test_expired_resets_at_uses_short_cooldown_instead_of_24h(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS == 5 * 60
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        expired_iso = (
            datetime.fromtimestamp(now - 600, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1 and now < 1_800_000_000.0 + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS:
                return httpx.Response(402, json=_402_payload(expired_iso))
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok content body", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        # t=0: Key 1 returns 402 with expired resets_at -> short 5m cooldown -> Key 2 succeeds
        await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        payload, ttl = store[f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"]
        assert ttl == fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS
        assert payload["cooldown_until"] == now + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS

        # t=4m: still in short cooldown -> uses Key 2
        now = 1_800_000_000.0 + 240.0
        await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})

        # t=5m+1s: short cooldown expired -> returns to Key 1 (well before 24h!)
        now = 1_800_000_000.0 + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS + 1.0
        await adapter.fetch(FetchRequest(urls=["https://site.example/3"]), {})

        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2, SECRET_KEY_1]

    async def test_multi_key_missing_balance_per_request_402_does_not_failover_or_trip_breaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_keys.append(_bearer_key(request))
            return httpx.Response(
                402,
                json={
                    "success": False,
                    "code": "insufficient_credits",
                    "error": "Insufficient credits to perform this request.",
                },
            )

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")
        for i in range(6):
            resp = await router.fetch(FetchRequest(urls=[f"https://site.example/req/{i}"]))
            err = resp.results[0].error
            assert err is not None
            assert err.type == WebErrorType.PROVIDER_ERROR
            assert err.message == "本次请求所需额度超过账户余额"
            assert err.provider_fault is False

        assert seen_keys == [SECRET_KEY_1] * 6
        assert fc_mod._INMEM_COOLDOWNS == {}
        assert store == {}
        assert not fc_entry.breaker.is_open()
        assert "firecrawl" not in router._budget_errors

    async def test_multi_key_missing_balance_exhaustion_402_cools_1h_and_fails_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert fc_mod._MISSING_BALANCE_COOLDOWN_SECONDS == 60 * 60
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(
                    402,
                    json={
                        "success": False,
                        "code": "insufficient_credits",
                        "error": "Insufficient credits",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"markdown": "ok content via backup", "metadata": {"statusCode": 200}},
                },
            )

        _install_mock_transport(monkeypatch, handler)
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/doc"]), {}
        )
        assert resp.results[0].ok
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2]
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        payload, ttl = store[f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"]
        assert ttl == fc_mod._MISSING_BALANCE_COOLDOWN_SECONDS
        assert payload["cooldown_until"] == now + fc_mod._MISSING_BALANCE_COOLDOWN_SECONDS

    async def test_primary_key_recovers_mid_request_retries_once_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        k1_resets_at = (
            datetime.fromtimestamp(now + 10, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        await fc_mod._mark_key_cooldown(fp1, resets_at=k1_resets_at, remaining=0)

        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal now
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_2:
                # Primary key expires while Key 2 request is in flight.
                now = 1_800_000_015.0
                store.pop(f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}", None)
                return httpx.Response(402, json=_402_payload())
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "# Recovered Primary\n\nSucceeded on mid-request retry.",
                        "metadata": {"sourceURL": "https://site.example/doc", "statusCode": 200},
                    },
                },
            )

        _install_mock_transport(monkeypatch, handler)
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/doc"]), {}
        )
        assert resp.results[0].ok
        assert seen_keys == [SECRET_KEY_2, SECRET_KEY_1]

    async def test_primary_key_recovers_mid_request_retry_also_402_fails_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        k1_resets_at = (
            datetime.fromtimestamp(now + 10, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        await fc_mod._mark_key_cooldown(fp1, resets_at=k1_resets_at, remaining=0)

        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal now
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_2:
                now = 1_800_000_015.0
                store.pop(f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}", None)
            return httpx.Response(402, json=_402_payload())

        _install_mock_transport(monkeypatch, handler)
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/doc"]), {}
        )
        assert not resp.results[0].ok
        assert resp.results[0].error is not None
        assert resp.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert seen_keys == [SECRET_KEY_2, SECRET_KEY_1]

    async def test_all_keys_cooled_displays_actual_cooldown_until_not_expired_or_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, _ = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        expired_iso = fc_mod._format_utc_iso(now - 600)
        expected_5m_iso = fc_mod._format_utc_iso(now + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS)

        def handler_expired(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload(expired_iso, remaining_credits=0))
            return httpx.Response(402, json=_402_payload(None, remaining_credits=0))

        _install_mock_transport(monkeypatch, handler_expired)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        r1 = await router.fetch(FetchRequest(urls=["https://site.example/expired-1"]))
        r2 = await router.fetch(FetchRequest(urls=["https://site.example/expired-2"]))
        for resp in (r1, r2):
            assert resp.results[0].error is not None
            assert resp.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
            assert expected_5m_iso in resp.results[0].error.message
            assert expired_iso not in resp.results[0].error.message
            assert "下个计费周期" not in resp.results[0].error.message

        # Also verify missing resets_at on all keys shows actual 1h/24h cooldown_until, never placeholder
        fc_mod._INMEM_COOLDOWNS.clear()
        cache, _ = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        expected_1h_iso = fc_mod._format_utc_iso(now + fc_mod._MISSING_BALANCE_COOLDOWN_SECONDS)

        def handler_missing_resets(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload(None, remaining_credits=None))
            return httpx.Response(402, json=_402_payload(None, remaining_credits=0))

        _install_mock_transport(monkeypatch, handler_missing_resets)
        router2 = FetchRouter(["inhouse", "firecrawl"])
        r3 = await router2.fetch(FetchRequest(urls=["https://site.example/missing-1"]))
        r4 = await router2.fetch(FetchRequest(urls=["https://site.example/missing-2"]))
        for resp in (r3, r4):
            assert resp.results[0].error is not None
            assert resp.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
            assert expected_1h_iso in resp.results[0].error.message
            assert "下个计费周期" not in resp.results[0].error.message

    async def test_both_keys_402_returns_budget_exceeded_and_router_fast_fails_until_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        resets_at = (
            datetime.fromtimestamp(now + 7200, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        http_calls: List[str] = []
        key1_exhausted = {"on": True}

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            http_calls.append(k)
            if k == SECRET_KEY_1 and not key1_exhausted["on"]:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "markdown": "# Key1 Back\n\nPrimary key recovered.",
                            "metadata": {"sourceURL": "https://site.example/p", "statusCode": 200},
                        },
                    },
                )
            return httpx.Response(402, json=_402_payload(resets_at))

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])

        with caplog.at_level(logging.DEBUG, logger="src.tools.web"):
            # 1st call: tries Key 1 (402) -> Key 2 (402) -> budget_exceeded
            r1 = await router.fetch(FetchRequest(urls=["https://site.example/1"]))
            assert r1.results[0].error is not None
            assert r1.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
            assert resets_at in r1.results[0].error.message
            assert http_calls == [SECRET_KEY_1, SECRET_KEY_2]
            assert "firecrawl" in router._budget_errors

            # 2nd call while both keys are in cooldown: router fast-fails with zero HTTP calls
            http_calls.clear()
            r2 = await router.fetch(FetchRequest(urls=["https://site.example/2"]))
            assert r2.results[0].error is not None
            assert r2.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
            assert resets_at in r2.results[0].error.message
            assert http_calls == []

            # Advance time past resets_at and expire Redis key: router clears budget error and uses Key 1
            now = now + 7201.0
            fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
            store.pop(f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}", None)
            key1_exhausted["on"] = False

            r3 = await router.fetch(FetchRequest(urls=["https://site.example/3"]))
            assert r3.results[0].ok
            assert http_calls == [SECRET_KEY_1]
            assert "firecrawl" not in router._budget_errors

        _assert_no_raw_keys_in_logs(caplog, [SECRET_KEY_1, SECRET_KEY_2])
        assert SECRET_KEY_1 not in r1.results[0].error.message
        assert SECRET_KEY_2 not in r1.results[0].error.message

    @pytest.mark.parametrize("primary_later", [True, False])
    async def test_all_keys_cooled_displays_earliest_recovery_time(
        self, monkeypatch: pytest.MonkeyPatch, primary_later: bool
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, _ = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        early_reset = (
            datetime.fromtimestamp(now + 86400, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        late_reset = (
            datetime.fromtimestamp(now + 5 * 86400, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            if k == SECRET_KEY_1:
                return httpx.Response(
                    402, json=_402_payload(late_reset if primary_later else early_reset)
                )
            return httpx.Response(
                402, json=_402_payload(early_reset if primary_later else late_reset)
            )

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        r1 = await router.fetch(FetchRequest(urls=["https://site.example/1"]))
        r2 = await router.fetch(FetchRequest(urls=["https://site.example/2"]))

        for resp in (r1, r2):
            assert resp.results[0].error is not None
            assert resp.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
            assert early_reset in resp.results[0].error.message
            assert late_reset not in resp.results[0].error.message

    async def test_cooldown_expiry_returns_to_primary_key_not_round_robin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        seen_keys: List[str] = []
        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1 and now < 1_800_000_060.0:
                resets_at = datetime.fromtimestamp(1_800_000_060.0, tz=timezone.utc).isoformat()
                return httpx.Response(402, json=_402_payload(resets_at))
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok content body", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        now = 1_800_000_030.0
        await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})
        now = 1_800_000_065.0
        await adapter.fetch(FetchRequest(urls=["https://site.example/3"]), {})
        await adapter.fetch(FetchRequest(urls=["https://site.example/4"]), {})

        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2, SECRET_KEY_1, SECRET_KEY_1]

    async def test_redis_error_falls_back_to_in_memory_without_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        broken_cache, _ = _make_fake_redis_cache(fail_reads=True, fail_writes=True)
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: broken_cache)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload())
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "fallback ok body", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        r1 = await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        r2 = await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})

        assert r1.results[0].ok and r2.results[0].ok
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2]

    async def test_redis_write_ok_then_read_fails_still_skips_cooled_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(402, json=_402_payload())
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok body content", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        # Write to Redis succeeds
        r1 = await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        assert r1.results[0].ok
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        assert f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}" in store

        # Subsequent Redis reads fail -> local copy must keep Key 1 skipped
        cache.get_strict.side_effect = RuntimeError("redis read down")
        r2 = await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})
        assert r2.results[0].ok
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2]

    async def test_redis_write_failed_then_recovers_keeps_local_cooldown_until_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)

        cache, store = _make_fake_redis_cache(fail_reads=True, fail_writes=True)
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        seen_keys: List[str] = []
        resets_at = (
            datetime.fromtimestamp(now + 120, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1 and now < 1_800_000_120.0:
                return httpx.Response(402, json=_402_payload(resets_at))
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok body content", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        # Redis down during 402 -> local cooldown written
        r1 = await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        assert r1.results[0].ok
        assert store == {}

        # Redis recovers (returns None for Key 1), still before expiry -> local copy MUST stay active!
        cache.get_strict.side_effect = lambda key: None
        now = 1_800_000_060.0
        r2 = await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})
        assert r2.results[0].ok

        # Past local expiry -> Key 1 recovers automatically
        now = 1_800_000_125.0
        r3 = await adapter.fetch(FetchRequest(urls=["https://site.example/3"]), {})
        assert r3.results[0].ok
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2, SECRET_KEY_1]

    async def test_final_cooldown_check_recovery_after_402_retries_once_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        k1_resets_at = fc_mod._format_utc_iso(now + 10)
        await fc_mod._mark_key_cooldown(fp1, resets_at=k1_resets_at, remaining=0)

        # Keep Key 1 cooled during loop 1 and loop 2, then expire it right at the
        # final get_firecrawl_pool_cooldown_state check.
        fp1_redis_key = f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"
        orig_pool_state = fc_mod.get_firecrawl_pool_cooldown_state

        async def _expire_on_final_check(*args: Any, **kwargs: Any) -> Any:
            nonlocal now
            now = 1_800_000_015.0
            store.pop(fp1_redis_key, None)
            return await orig_pool_state(*args, **kwargs)

        monkeypatch.setattr(fc_mod, "get_firecrawl_pool_cooldown_state", _expire_on_final_check)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_2:
                return httpx.Response(402, json=_402_payload())
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "# Late Recovery\n\nRetried on final pool check.",
                        "metadata": {"sourceURL": "https://site.example/doc", "statusCode": 200},
                    },
                },
            )

        _install_mock_transport(monkeypatch, handler)
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/doc"]), {}
        )
        assert resp.results[0].ok
        assert seen_keys == [SECRET_KEY_2, SECRET_KEY_1]

        # Bounded to at most one retry even with 3 keys when two keys recover at the end
        fc_mod._INMEM_COOLDOWNS.clear()
        store.clear()
        monkeypatch.setenv("FIRECRAWL_API_KEY_3", SECRET_KEY_3)
        now = 1_800_000_000.0
        fp3 = fc_mod._key_fingerprint(SECRET_KEY_3)
        fp3_redis_key = f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp3}"
        await fc_mod._mark_key_cooldown(fp1, resets_at=fc_mod._format_utc_iso(now + 10), remaining=0)
        await fc_mod._mark_key_cooldown(fp3, resets_at=fc_mod._format_utc_iso(now + 10), remaining=0)

        async def _both_expire_on_final_check(*args: Any, **kwargs: Any) -> Any:
            nonlocal now
            now = 1_800_000_015.0
            store.pop(fp1_redis_key, None)
            store.pop(fp3_redis_key, None)
            return await orig_pool_state(*args, **kwargs)

        monkeypatch.setattr(
            fc_mod, "get_firecrawl_pool_cooldown_state", _both_expire_on_final_check
        )
        seen_keys.clear()

        def handler_all_402(request: httpx.Request) -> httpx.Response:
            seen_keys.append(_bearer_key(request))
            return httpx.Response(402, json=_402_payload())

        _install_mock_transport(monkeypatch, handler_all_402)
        resp2 = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/doc"]), {}
        )
        assert resp2.results[0].error is not None
        assert resp2.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert seen_keys == [SECRET_KEY_2, SECRET_KEY_1]

    async def test_final_cooldown_check_recovery_when_all_skipped_retries_once_instead_of_24h_402(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        fp2 = fc_mod._key_fingerprint(SECRET_KEY_2)
        fp1_redis_key = f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"
        await fc_mod._mark_key_cooldown(fp1, resets_at=fc_mod._format_utc_iso(now + 10), remaining=0)
        await fc_mod._mark_key_cooldown(fp2, resets_at=fc_mod._format_utc_iso(now + 3600), remaining=0)

        orig_pool_state = fc_mod.get_firecrawl_pool_cooldown_state

        async def _expire_k1_on_final_check(*args: Any, **kwargs: Any) -> Any:
            nonlocal now
            now = 1_800_000_015.0
            store.pop(fp1_redis_key, None)
            return await orig_pool_state(*args, **kwargs)

        monkeypatch.setattr(
            fc_mod, "get_firecrawl_pool_cooldown_state", _expire_k1_on_final_check
        )
        seen_keys: List[str] = []

        def handler_ok(request: httpx.Request) -> httpx.Response:
            seen_keys.append(_bearer_key(request))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"markdown": "ok after boundary recovery", "metadata": {"statusCode": 200}},
                },
            )

        _install_mock_transport(monkeypatch, handler_ok)
        resp = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/boundary-ok"]), {}
        )
        assert resp.results[0].ok
        assert seen_keys == [SECRET_KEY_1]

        # If that single retry also returns 402 (with expired resets_at -> 5m cooldown),
        # the returned BUDGET_EXCEEDED error reports the 5m cooldown_until, NOT +24h.
        fc_mod._INMEM_COOLDOWNS.clear()
        store.clear()
        now = 1_800_000_000.0
        await fc_mod._mark_key_cooldown(fp1, resets_at=fc_mod._format_utc_iso(now + 10), remaining=0)
        await fc_mod._mark_key_cooldown(fp2, resets_at=fc_mod._format_utc_iso(now + 3600), remaining=0)
        seen_keys.clear()
        expired_iso = fc_mod._format_utc_iso(1_800_000_015.0 - 600)
        expected_5m_iso = fc_mod._format_utc_iso(1_800_000_015.0 + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS)
        wrong_24h_iso = fc_mod._format_utc_iso(1_800_000_015.0 + fc_mod._DEFAULT_COOLDOWN_SECONDS)

        def handler_402(request: httpx.Request) -> httpx.Response:
            seen_keys.append(_bearer_key(request))
            return httpx.Response(402, json=_402_payload(expired_iso, remaining_credits=0))

        _install_mock_transport(monkeypatch, handler_402)
        resp_err = await fc_mod.FirecrawlFetchAdapter().fetch(
            FetchRequest(urls=["https://site.example/boundary-402"]), {}
        )
        err = resp_err.results[0].error
        assert err is not None
        assert err.type == WebErrorType.BUDGET_EXCEEDED
        assert seen_keys == [SECRET_KEY_1]
        assert expected_5m_iso in err.message
        assert wrong_24h_iso not in err.message

    async def test_cooldown_deadline_only_extends_in_redis_and_local_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        redis_key = f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"
        far_until = now + 5 * 86400
        far_iso = fc_mod._format_utc_iso(far_until)

        # Worker A records explicit 5-day cooldown
        await fc_mod._mark_key_cooldown(fp1, resets_at=far_iso, remaining=0)
        assert store[redis_key][0]["cooldown_until"] == far_until

        # Worker B (empty local memory) hits a 402 without resets_at (1h fallback):
        # Redis must keep the later 5-day deadline, and Worker B learns it locally too.
        fc_mod._INMEM_COOLDOWNS.clear()
        recorded = await fc_mod._mark_key_cooldown(fp1, resets_at=None, remaining=None)
        assert store[redis_key][0]["cooldown_until"] == far_until
        assert store[redis_key][1] == 5 * 86400
        assert recorded["cooldown_until"] == far_until
        assert fc_mod._INMEM_COOLDOWNS[fp1]["cooldown_until"] == far_until

        # Local memory also refuses to regress to a shorter cooldown
        expired_iso = fc_mod._format_utc_iso(now - 60)
        recorded2 = await fc_mod._mark_key_cooldown(fp1, resets_at=expired_iso, remaining=0)
        assert recorded2["cooldown_until"] == far_until
        assert fc_mod._INMEM_COOLDOWNS[fp1]["cooldown_until"] == far_until

        # Extending from a shorter cooldown (1h) to a longer one (5d) updates both Redis & local
        fc_mod._INMEM_COOLDOWNS.clear()
        store.clear()
        await fc_mod._mark_key_cooldown(fp1, resets_at=None, remaining=None)
        assert store[redis_key][0]["cooldown_until"] == now + fc_mod._MISSING_BALANCE_COOLDOWN_SECONDS
        await fc_mod._mark_key_cooldown(fp1, resets_at=far_iso, remaining=0)
        assert store[redis_key][0]["cooldown_until"] == far_until
        assert fc_mod._INMEM_COOLDOWNS[fp1]["cooldown_until"] == far_until

    async def test_redis_read_caches_cooldown_locally_for_subsequent_read_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        store[f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}"] = (
            {
                "fingerprint": fp1,
                "cooldown_until": now + 3600,
                "resets_at": fc_mod._format_utc_iso(now + 3600),
                "reason": "insufficient_credits",
            },
            3600,
        )
        assert fp1 not in fc_mod._INMEM_COOLDOWNS

        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_keys.append(_bearer_key(request))
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "ok via backup", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlFetchAdapter()

        # First read hits Redis and caches cooldown in local _INMEM_COOLDOWNS
        r1 = await adapter.fetch(FetchRequest(urls=["https://site.example/1"]), {})
        assert r1.results[0].ok
        assert fc_mod._INMEM_COOLDOWNS[fp1]["cooldown_until"] == now + 3600

        # Subsequent Redis read fails -> worker still remembers Key 1 cooldown locally
        cache.get_strict.side_effect = RuntimeError("redis read down")
        r2 = await adapter.fetch(FetchRequest(urls=["https://site.example/2"]), {})
        assert r2.results[0].ok
        assert seen_keys == [SECRET_KEY_2, SECRET_KEY_2]

    async def test_router_resets_half_open_breaker_when_firecrawl_key_recovers_from_cooldown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        now = 1_800_000_000.0
        monkeypatch.setattr(fc_mod.time, "time", lambda: now)
        monkeypatch.setattr(breaker_mod.time, "time", lambda: now)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")

        # Put both keys into a 5m cooldown (> 60s breaker recovery_timeout) and trip the
        # breaker into OPEN (simulating >=5 concurrent requests before cooldown settled).
        expired_iso = fc_mod._format_utc_iso(now - 60)
        fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
        fp2 = fc_mod._key_fingerprint(SECRET_KEY_2)
        await fc_mod._mark_key_cooldown(fp1, resets_at=expired_iso, remaining=0)
        await fc_mod._mark_key_cooldown(fp2, resets_at=expired_iso, remaining=0)
        for _ in range(fc_entry.breaker.failure_threshold):
            await fc_entry.breaker.record_failure(reason="402 exhausted")
        assert fc_entry.breaker.state is CircuitState.OPEN
        router._budget_errors["firecrawl"] = WebError(
            type=WebErrorType.BUDGET_EXCEEDED,
            message="cooldown notice",
            http_status=402,
            retryable=True,
            provider_fault=True,
            native_kind="insufficient_credits",
        )

        # Advance past 5m cooldown (> 60s breaker recovery_timeout): check_state() moves
        # breaker OPEN -> HALF_OPEN before the cooldown check clears _budget_errors.
        now = now + fc_mod._EXPIRED_RESET_COOLDOWN_SECONDS + 5.0
        store.pop(f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}", None)

        mode = {"status": 502}

        def handler(request: httpx.Request) -> httpx.Response:
            if mode["status"] == 502:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "recovered content", "metadata": {"statusCode": 200}}},
            )

        _install_mock_transport(monkeypatch, handler)

        # Single transient 502 must leave the reset breaker CLOSED (failure_count=1),
        # NOT re-open it from HALF_OPEN with doubled recovery_timeout!
        r_transient = await router.fetch(FetchRequest(urls=["https://site.example/transient"]))
        assert not r_transient.results[0].ok
        assert "firecrawl" not in router._budget_errors
        assert fc_entry.breaker.state is CircuitState.CLOSED
        assert fc_entry.breaker.failure_count == 1

        mode["status"] = 200
        r_ok = await router.fetch(FetchRequest(urls=["https://site.example/ok"]))
        assert r_ok.results[0].ok
        assert fc_entry.breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
class TestCrawlKeyPinning:
    async def test_crawl_status_and_cancel_use_originating_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        calls: List[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            calls.append((request.method, str(request.url), k))
            if request.method == "POST" and request.url.path == "/v2/crawl":
                if k == SECRET_KEY_1:
                    return httpx.Response(402, json=_402_payload())
                return httpx.Response(200, json={"success": True, "id": "crawl-job-2"})
            if request.method == "GET" and request.url.path == "/v2/crawl/crawl-job-2":
                assert k == SECRET_KEY_2, "crawl_status must use the originating key"
                if "skip=1" in str(request.url):
                    return httpx.Response(
                        200,
                        json={
                            "status": "completed",
                            "data": [{"markdown": "page 2 body content", "metadata": {"url": "https://site.example/2", "statusCode": 200}}],
                            "next": None,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "total": 2,
                        "completed": 2,
                        "data": [{"markdown": "page 1 body content", "metadata": {"url": "https://site.example/1", "statusCode": 200}}],
                        "next": "https://api.firecrawl.dev/v2/crawl/crawl-job-2?skip=1",
                    },
                )
            if request.method == "DELETE" and request.url.path == "/v2/crawl/crawl-job-2":
                assert k == SECRET_KEY_2, "cancel_crawl must use the originating key"
                return httpx.Response(200, json={"success": True})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlCrawlAdapter()

        with caplog.at_level(logging.DEBUG, logger="src.tools.web.providers.firecrawl"):
            job = await adapter.start_crawl(CrawlRequest(url="https://site.example", limit=5), {})
            assert job == CrawlJob(id="crawl-job-2", provider="firecrawl")
            if fc_mod._BACKGROUND_TASKS:
                await asyncio.gather(*list(fc_mod._BACKGROUND_TASKS))

            # Even if Key 1 recovers AND Key 2 enters cooldown, status & cancel MUST stick to Key 2!
            fp1 = fc_mod._key_fingerprint(SECRET_KEY_1)
            fp2 = fc_mod._key_fingerprint(SECRET_KEY_2)
            store.pop(f"{fc_mod._COOLDOWN_REDIS_PREFIX}{fp1}", None)
            fc_mod._INMEM_COOLDOWNS.pop(fp1, None)
            await fc_mod._mark_key_cooldown(fp2)

            # Simulate a fresh adapter instance / different worker reading from Redis
            fc_mod._INMEM_CRAWL_KEYS.clear()
            fresh_adapter = fc_mod.FirecrawlCrawlAdapter()
            status = await fresh_adapter.crawl_status(job.id)
            assert status.state is CrawlState.COMPLETED
            assert len(status.pages) == 2

            await fresh_adapter.cancel_crawl(job.id)

        assert [c[2] for c in calls] == [
            SECRET_KEY_1,
            SECRET_KEY_2,
            SECRET_KEY_2,
            SECRET_KEY_2,
            SECRET_KEY_2,
        ]
        _assert_no_raw_keys_in_logs(caplog, [SECRET_KEY_1, SECRET_KEY_2])

    async def test_start_crawl_returns_job_id_without_awaiting_redis_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)

        redis_started = asyncio.Event()
        allow_redis_fail = asyncio.Event()
        cache = create_autospec(RedisCacheClient, instance=True)
        cache.enabled = True
        cache.client = object()

        async def _blocking_failing_set(key: str, value: Any, ttl: Optional[int] = None) -> bool:
            redis_started.set()
            await allow_redis_fail.wait()
            raise RuntimeError("redis write timed out")

        cache.set.side_effect = _blocking_failing_set
        cache.get_strict.return_value = None
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "id": "crawl-fast-1"})

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlCrawlAdapter()

        # Must return job.id immediately even though Redis set is blocked
        job = await asyncio.wait_for(
            adapter.start_crawl(CrawlRequest(url="https://site.example", limit=5), {}),
            timeout=1.0,
        )
        assert job == CrawlJob(id="crawl-fast-1", provider="firecrawl")
        assert fc_mod._INMEM_CRAWL_KEYS["crawl-fast-1"] == fc_mod._key_fingerprint(SECRET_KEY_1)

        with caplog.at_level(logging.WARNING, logger="src.tools.web.providers.firecrawl"):
            await redis_started.wait()
            allow_redis_fail.set()
            if fc_mod._BACKGROUND_TASKS:
                await asyncio.gather(*list(fc_mod._BACKGROUND_TASKS))

        assert any("Redis write failed for Firecrawl crawl key map" in r.getMessage() for r in caplog.records)
        _assert_no_raw_keys_in_logs(caplog, [SECRET_KEY_1, SECRET_KEY_2])

    async def test_crawl_status_without_mapping_falls_back_on_404_and_re_records_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        # Simulate lost mapping (Redis has no entry and in-memory LRU was evicted),
        # and also put Key 2 in cooldown to prove fallback still polls the originating key.
        fp2 = fc_mod._key_fingerprint(SECRET_KEY_2)
        await fc_mod._mark_key_cooldown(fp2)

        calls: List[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            calls.append((k, str(request.url)))
            if k == SECRET_KEY_1:
                return httpx.Response(404, json={"success": False, "error": "Job not found"})
            if "skip=1" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "data": [
                            {
                                "markdown": "page 2 body content",
                                "metadata": {"url": "https://site.example/2", "statusCode": 200},
                            }
                        ],
                        "next": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "scraping",
                    "total": 2,
                    "completed": 1,
                    "data": [
                        {
                            "markdown": "page 1 body content",
                            "metadata": {"url": "https://site.example/1", "statusCode": 200},
                        }
                    ],
                    "next": "https://api.firecrawl.dev/v2/crawl/lost-job-2?skip=1",
                },
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlCrawlAdapter()
        status = await adapter.crawl_status("lost-job-2")
        if fc_mod._BACKGROUND_TASKS:
            await asyncio.gather(*list(fc_mod._BACKGROUND_TASKS))

        assert status.state is CrawlState.RUNNING
        assert len(status.pages) == 2
        # Page 1 tried Key 1 (404) -> Key 2 (200) and re-pinned Key 2; pagination went straight to Key 2.
        assert [k for k, _ in calls] == [SECRET_KEY_1, SECRET_KEY_2, SECRET_KEY_2]
        assert fc_mod._INMEM_CRAWL_KEYS["lost-job-2"] == fp2
        assert store[f"{fc_mod._CRAWL_KEY_REDIS_PREFIX}lost-job-2"][0] == {"fingerprint": fp2}

        # Next status poll uses the restored mapping directly without hitting Key 1
        calls.clear()
        await adapter.crawl_status("lost-job-2")
        assert [k for k, _ in calls] == [SECRET_KEY_2, SECRET_KEY_2]

    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_crawl_status_without_mapping_skips_a_revoked_key(
        self, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, _store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        calls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            calls.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(status_code, json={"success": False, "error": "Unauthorized"})
            return httpx.Response(
                200,
                json={"status": "completed", "total": 0, "completed": 0, "data": [], "next": None},
            )

        _install_mock_transport(monkeypatch, handler)
        adapter = fc_mod.FirecrawlCrawlAdapter()
        status = await adapter.crawl_status("revoked-primary-job")
        if fc_mod._BACKGROUND_TASKS:
            await asyncio.gather(*list(fc_mod._BACKGROUND_TASKS))

        assert status.state is CrawlState.COMPLETED
        assert calls == [SECRET_KEY_1, SECRET_KEY_2]
        assert fc_mod._INMEM_CRAWL_KEYS["revoked-primary-job"] == fc_mod._key_fingerprint(SECRET_KEY_2)

    async def test_cancel_crawl_without_mapping_tries_keys_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("FIRECRAWL_API_KEY_2", SECRET_KEY_2)
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)
        seen_keys: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            k = _bearer_key(request)
            seen_keys.append(k)
            if k == SECRET_KEY_1:
                return httpx.Response(404, json={"success": False, "error": "Job not found"})
            return httpx.Response(200, json={"success": True})

        _install_mock_transport(monkeypatch, handler)
        await fc_mod.FirecrawlCrawlAdapter().cancel_crawl("unmapped-job-9")
        if fc_mod._BACKGROUND_TASKS:
            await asyncio.gather(*list(fc_mod._BACKGROUND_TASKS))
        fp2 = fc_mod._key_fingerprint(SECRET_KEY_2)
        assert seen_keys == [SECRET_KEY_1, SECRET_KEY_2]
        assert fc_mod._INMEM_CRAWL_KEYS["unmapped-job-9"] == fp2
        assert store[f"{fc_mod._CRAWL_KEY_REDIS_PREFIX}unmapped-job-9"][0] == {"fingerprint": fp2}


@pytest.mark.asyncio
class TestSingleKeyPreservesOriginalBehavior:
    async def test_single_key_402_does_not_cooldown_and_recovers_on_half_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        succeed = {"on": False}
        seen_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_calls
            seen_calls += 1
            if request.url.path == "/v2/crawl":
                return httpx.Response(200, json={"success": True, "id": "single-crawl-1"})
            if succeed["on"]:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "markdown": "# Back Online\n\nCredits topped up.",
                            "metadata": {"sourceURL": "https://site.example/ok", "statusCode": 200},
                        },
                    },
                )
            return httpx.Response(
                402,
                json=_402_payload("2026-11-26T00:00:00Z"),
            )

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        # Single-key start_crawl must not write crawl-key mapping to memory or Redis
        job = await fc_mod.FirecrawlCrawlAdapter().start_crawl(
            CrawlRequest(url="https://site.example", limit=5), {}
        )
        assert job == CrawlJob(id="single-crawl-1", provider="firecrawl")
        assert fc_mod._INMEM_CRAWL_KEYS == {}
        assert store == {}
        seen_calls = 0

        router = FetchRouter(["inhouse", "firecrawl"])
        fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")

        # Single key keeps 5-failure breaker behavior and writes no per-key cooldown
        for i in range(5):
            r = await router.fetch(FetchRequest(urls=[f"https://site.example/{i}"]))
            assert r.results[0].error is not None
            assert r.results[0].error.type == WebErrorType.BUDGET_EXCEEDED

        assert seen_calls == 5
        assert store == {}
        assert fc_mod._INMEM_COOLDOWNS == {}
        assert fc_entry.breaker.is_open()

        # Breaker open skips Firecrawl with 402 notice
        skipped = await router.fetch(FetchRequest(urls=["https://site.example/skip"]))
        assert skipped.results[0].error is not None
        assert skipped.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert seen_calls == 5

        # Half-open recovery succeeds and clears _budget_errors
        fc_entry.breaker.last_failure_time = 1.0
        succeed["on"] = True
        ok = await router.fetch(FetchRequest(urls=["https://site.example/ok"]))
        assert ok.results[0].ok
        assert seen_calls == 6
        assert "firecrawl" not in router._budget_errors

    async def test_single_key_positive_remaining_402_preserves_original_budget_and_breaker_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", SECRET_KEY_1)
        monkeypatch.setenv("DALTON_ENABLED", "1")
        cache, store = _make_fake_redis_cache()
        monkeypatch.setattr(redis_cache_mod, "get_cache_client", lambda: cache)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                402,
                json=_402_payload(
                    "2026-11-26T00:00:00Z",
                    remaining_credits=25,
                    error="Insufficient credits for this request",
                ),
            )

        _install_mock_transport(monkeypatch, handler)

        async def _inhouse_timeout(
            self: InhouseFetchAdapter, req: FetchRequest, native_params: dict[str, Any]
        ) -> FetchResponse:
            return FetchResponse(
                results=[
                    FetchResult(
                        url=u,
                        error=WebError(type=WebErrorType.TIMEOUT, message="timed out"),
                    )
                    for u in req.urls
                ],
                provider="inhouse",
            )

        monkeypatch.setattr(
            InhouseFetchAdapter,
            "fetch",
            create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout),
        )

        router = FetchRouter(["inhouse", "firecrawl"])
        fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")

        for i in range(5):
            r = await router.fetch(FetchRequest(urls=[f"https://site.example/big/{i}"]))
            err = r.results[0].error
            assert err is not None
            assert err.type == WebErrorType.BUDGET_EXCEEDED
            assert err.message == "本次请求所需额度超过 Firecrawl 余额"
            assert err.provider_fault is True
            assert err.native_kind == "insufficient_credits_for_request"

        assert fc_entry.breaker.is_open()
        assert "firecrawl" in router._budget_errors
        assert store == {}
        assert fc_mod._INMEM_COOLDOWNS == {}
