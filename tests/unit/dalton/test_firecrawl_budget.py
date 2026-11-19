"""Unit tests for Dalton Firecrawl budget controls: no auto->stealth double billing and 402 notices."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

import src.tools.web.fetch as fetch_mod
import src.tools.web.manifest as wm
import src.tools.web.providers.firecrawl as fc_mod
from src.dalton.settings import get_dalton_config
from src.tools.web.breaker import CircuitState
from src.tools.web.providers.inhouse import InhouseFetchAdapter
from src.tools.web.router import FetchRouter
from src.tools.web.types import (
    CrawlRequest,
    FetchRequest,
    FetchResponse,
    FetchResult,
    WebError,
    WebErrorType,
    WebToolError,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    wm.get_web_providers.cache_clear()
    get_dalton_config.cache_clear()
    yield
    wm.get_web_providers.cache_clear()
    get_dalton_config.cache_clear()


@pytest.mark.asyncio
class TestFirecrawlNoAutoToStealthDoubleCharge:
    async def test_dalton_enabled_auto_403_sends_single_firecrawl_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        seen_proxies: list[str | None] = []

        async def _fake_scrape(
            self: fc_mod.FirecrawlAPI,
            url: str,
            proxy: str | None = None,
            max_age_seconds: int | None = None,
        ) -> dict[str, Any]:
            seen_proxies.append(proxy)
            return {
                "success": True,
                "data": {"markdown": "", "metadata": {"statusCode": 403}},
            }

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_fake_scrape),
        )

        router = FetchRouter(["firecrawl"])
        resp = await router.fetch(FetchRequest(urls=["https://protected.example/doc"]))

        assert not resp.results[0].ok
        assert resp.results[0].error is not None
        assert resp.results[0].error.type == WebErrorType.ANTI_BOT
        assert seen_proxies == ["auto"]

    async def test_dalton_disabled_auto_403_keeps_upstream_stealth_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "0")
        seen_proxies: list[str | None] = []

        async def _fake_scrape(
            self: fc_mod.FirecrawlAPI,
            url: str,
            proxy: str | None = None,
            max_age_seconds: int | None = None,
        ) -> dict[str, Any]:
            seen_proxies.append(proxy)
            if proxy == "stealth":
                return {
                    "success": True,
                    "data": {
                        "markdown": "# Bypassed\n\nFull page text after stealth.",
                        "metadata": {"title": "Bypassed", "sourceURL": url, "statusCode": 200},
                    },
                }
            return {
                "success": True,
                "data": {"markdown": "", "metadata": {"statusCode": 403}},
            }

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_fake_scrape),
        )

        router = FetchRouter(["firecrawl"])
        resp = await router.fetch(FetchRequest(urls=["https://protected.example/doc"]))

        assert resp.results[0].ok
        assert seen_proxies == ["auto", "stealth"]


@pytest.mark.asyncio
class TestFirecrawl402MessagesAndBreakerNotice:
    @pytest.mark.parametrize(
        ("body", "expected_msg"),
        [
            (
                {
                    "success": False,
                    "code": "insufficient_credits",
                    "error": "Insufficient credits",
                    "remaining_credits": 0,
                    "resets_at": "2026-11-26T00:00:00Z",
                },
                "Firecrawl 本月额度已用完，将于 2026-11-26T00:00:00Z 重置，请改用浏览器抓取",
            ),
            (
                {
                    "success": False,
                    "code": "insufficient_credits",
                    "error": "Insufficient credits",
                    "remainingCredits": -5,
                },
                "Firecrawl 本月额度已用完，将于 下个计费周期 重置，请改用浏览器抓取",
            ),
            (
                {
                    "success": False,
                    "code": "insufficient_credits",
                    "error": "Insufficient credits to perform this request.",
                },
                "Firecrawl 返回额度不足（402）：Insufficient credits to perform this request.",
            ),
            (
                {
                    "success": False,
                    "error": "Insufficient credits",
                },
                "Firecrawl 返回额度不足（402）：Insufficient credits",
            ),
            (
                {
                    "success": False,
                    "code": "insufficient_credits",
                    "error": "Insufficient credits for this request",
                    "remaining_credits": 25,
                },
                "本次请求所需额度超过 Firecrawl 余额",
            ),
        ],
    )
    async def test_scrape_402_insufficient_credits_variants(
        self,
        monkeypatch: pytest.MonkeyPatch,
        body: dict[str, Any],
        expected_msg: str,
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        url = "https://target.example/report"
        response = httpx.Response(
            402,
            json=body,
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )

        async def _raise_402(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {response.text}",
                request=response.request,
                response=response,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_raise_402),
        )

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
        monkeypatch.setattr(
            fetch_mod,
            "_get_cache_client",
            create_autospec(fetch_mod._get_cache_client, return_value=None),
        )
        service = fetch_mod.FetchService(chain=["inhouse", "firecrawl"])
        monkeypatch.setattr(fetch_mod, "get_fetch_service", lambda: service)

        content, artifact = await fetch_mod.web_fetch(
            url=url,
            prompt="summarize",
            model="test-model",
            use_cache=False,
            include_sitemap=False,
        )

        assert expected_msg in content
        assert artifact["error"] == WebErrorType.BUDGET_EXCEEDED.value

    async def test_402_platform_managed_credits_keeps_original_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        response = httpx.Response(
            402,
            json={
                "success": False,
                "code": "platform_managed_credits",
                "error": "Insufficient credits: contact your platform administrator.",
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )

        async def _raise_402(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {response.text}",
                request=response.request,
                response=response,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_raise_402),
        )

        router = FetchRouter(["firecrawl"])
        resp = await router.fetch(FetchRequest(urls=["https://target.example/doc"]))
        err = resp.results[0].error
        assert err is not None
        assert err.type == WebErrorType.BUDGET_EXCEEDED
        assert "本月额度已用完" not in err.message
        assert "platform_managed_credits" in err.message

    async def test_402_not_rewritten_when_dalton_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "0")
        response = httpx.Response(
            402,
            json={
                "success": False,
                "code": "insufficient_credits",
                "error": "Insufficient credits",
                "resets_at": "2026-11-26T00:00:00Z",
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )

        async def _raise_402(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {response.text}",
                request=response.request,
                response=response,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_raise_402),
        )

        router = FetchRouter(["firecrawl"])
        resp = await router.fetch(FetchRequest(urls=["https://target.example/doc"]))
        err = resp.results[0].error
        assert err is not None
        assert err.type == WebErrorType.BUDGET_EXCEEDED
        assert "本月额度已用完" not in err.message
        assert "Insufficient credits" in err.message

    async def test_crawl_402_distinguishes_exhausted_vs_positive_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        adapter = fc_mod.FirecrawlCrawlAdapter()

        resp_exceeded = httpx.Response(
            402,
            json={
                "success": False,
                "code": "insufficient_credits",
                "remaining_credits": 10,
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/crawl"),
        )

        async def _raise_exceeded(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {resp_exceeded.text}",
                request=resp_exceeded.request,
                response=resp_exceeded,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "start_crawl",
            create_autospec(fc_mod.FirecrawlAPI.start_crawl, side_effect=_raise_exceeded),
        )

        with pytest.raises(WebToolError) as exc_info:
            await adapter.start_crawl(CrawlRequest(url="https://target.example", limit=50), {})
        assert exc_info.value.error.message == "本次请求所需额度超过 Firecrawl 余额"

    async def test_402_breaker_notice_only_attaches_to_firecrawl_skipped_urls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        response = httpx.Response(
            402,
            json={
                "success": False,
                "code": "insufficient_credits",
                "remaining_credits": 0,
                "resets_at": "2026-11-26T00:00:00Z",
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )

        async def _raise_402(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {response.text}",
                request=response.request,
                response=response,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_raise_402),
        )

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
        for i in range(5):
            r = await router.fetch(FetchRequest(urls=[f"https://target.example/{i}"]))
            assert r.results[0].error is not None
            assert r.results[0].error.type == WebErrorType.BUDGET_EXCEEDED

        # Mixed batch while Firecrawl breaker is open:
        # - YouTube URL is forced to inhouse -> must keep inhouse TIMEOUT, NOT Firecrawl 402.
        # - Normal URL would have gone to Firecrawl -> must get Firecrawl 402 notice.
        yt_url = "https://www.youtube.com/watch?v=abc"
        normal_url = "https://target.example/skipped"
        mixed = await router.fetch(FetchRequest(urls=[yt_url, normal_url]))

        by_url = {r.url: r for r in mixed.results}
        assert by_url[yt_url].error is not None
        assert by_url[yt_url].error.type == WebErrorType.TIMEOUT
        assert "Firecrawl" not in by_url[yt_url].error.message

        assert by_url[normal_url].error is not None
        assert by_url[normal_url].error.type == WebErrorType.BUDGET_EXCEEDED
        assert (
            "Firecrawl 本月额度已用完，将于 2026-11-26T00:00:00Z 重置，请改用浏览器抓取"
            in by_url[normal_url].error.message
        )

    async def test_inflight_success_while_breaker_open_keeps_402_notice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        response = httpx.Response(
            402,
            json={
                "success": False,
                "code": "insufficient_credits",
                "remaining_credits": 0,
                "resets_at": "2026-11-26T00:00:00Z",
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )
        inflight_entered = asyncio.Event()
        release_inflight = asyncio.Event()

        async def _scrape(
            self: fc_mod.FirecrawlAPI,
            url: str,
            proxy: str | None = None,
            max_age_seconds: int | None = None,
        ) -> dict[str, Any]:
            if url.endswith("/inflight"):
                inflight_entered.set()
                await release_inflight.wait()
                return {
                    "success": True,
                    "data": {
                        "markdown": "# Late success\n\nArrived after breaker opened.",
                        "metadata": {"sourceURL": url, "statusCode": 200},
                    },
                }
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {response.text}",
                request=response.request,
                response=response,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_scrape),
        )

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

        # Start an in-flight request before the breaker opens.
        inflight_task = asyncio.create_task(
            router.fetch(FetchRequest(urls=["https://target.example/inflight"]))
        )
        await inflight_entered.wait()

        # Trip the breaker with 5 concurrent 402 failures.
        for i in range(5):
            r = await router.fetch(FetchRequest(urls=[f"https://target.example/{i}"]))
            assert r.results[0].error is not None
            assert r.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert fc_entry.breaker.state is CircuitState.OPEN

        # Late in-flight success finishes while breaker is OPEN: must NOT clear 402 notice.
        release_inflight.set()
        late_resp = await inflight_task
        assert late_resp.results[0].ok
        assert fc_entry.breaker.state is CircuitState.OPEN

        after_resp = await router.fetch(FetchRequest(urls=["https://target.example/after"]))
        assert after_resp.results[0].error is not None
        assert after_resp.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert (
            "Firecrawl 本月额度已用完，将于 2026-11-26T00:00:00Z 重置，请改用浏览器抓取"
            in after_resp.results[0].error.message
        )

    async def test_mixed_402_and_429_batch_tripping_breaker_keeps_402_notice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
        monkeypatch.setenv("DALTON_ENABLED", "1")
        resp_402 = httpx.Response(
            402,
            json={
                "success": False,
                "code": "insufficient_credits",
                "remaining_credits": 0,
                "resets_at": "2026-11-26T00:00:00Z",
            },
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )
        resp_429 = httpx.Response(
            429,
            json={"success": False, "error": "Rate limit exceeded"},
            request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
        )

        async def _scrape(
            self: fc_mod.FirecrawlAPI,
            url: str,
            proxy: str | None = None,
            max_age_seconds: int | None = None,
        ) -> dict[str, Any]:
            if url.endswith("/429"):
                raise httpx.HTTPStatusError(
                    f"Firecrawl API error 429: {resp_429.text}",
                    request=resp_429.request,
                    response=resp_429,
                )
            raise httpx.HTTPStatusError(
                f"Firecrawl API error 402: {resp_402.text}",
                request=resp_402.request,
                response=resp_402,
            )

        monkeypatch.setattr(
            fc_mod.FirecrawlAPI,
            "scrape",
            create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_scrape),
        )

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

        # 4 sequential 402s, then a 5th batch mixing 402 and 429 trips the breaker.
        for i in range(4):
            r = await router.fetch(FetchRequest(urls=[f"https://target.example/{i}"]))
            assert r.results[0].error is not None
            assert r.results[0].error.type == WebErrorType.BUDGET_EXCEEDED

        mixed_5th = await router.fetch(
            FetchRequest(urls=["https://target.example/402", "https://target.example/429"])
        )
        assert mixed_5th.results[0].error is not None
        assert mixed_5th.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert fc_entry.breaker.state is CircuitState.OPEN

        # Subsequent skipped request still surfaces the 402 reset notice.
        resp6 = await router.fetch(FetchRequest(urls=["https://target.example/6"]))
        assert resp6.results[0].error is not None
        assert resp6.results[0].error.type == WebErrorType.BUDGET_EXCEEDED
        assert (
            "Firecrawl 本月额度已用完，将于 2026-11-26T00:00:00Z 重置，请改用浏览器抓取"
            in resp6.results[0].error.message
        )



@pytest.mark.asyncio
async def test_generic_402_keeps_a_notice_and_a_later_success_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 402 without remaining_credits still leaves a notice when it opens the
    breaker; once traffic flows again, a success clears it, so a later trip that
    is not about credits does not resurface the stale notice."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("DALTON_ENABLED", "1")
    resp_402 = httpx.Response(
        402,
        json={"success": False, "error": "Insufficient credits to perform this request."},
        request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
    )
    succeed = {"on": False}

    async def _scrape(self, url, proxy=None, max_age_seconds=None):
        if succeed["on"]:
            return {"success": True, "data": {"markdown": "# Recovered\n\nFull page text after credits returned.", "metadata": {"sourceURL": url, "statusCode": 200}}}
        raise httpx.HTTPStatusError("Firecrawl API error 402", request=resp_402.request, response=resp_402)

    async def _inhouse_timeout(self, req, native_params):
        return FetchResponse(
            results=[FetchResult(url=u, error=WebError(type=WebErrorType.TIMEOUT, message="timed out")) for u in req.urls],
            provider="inhouse",
        )

    monkeypatch.setattr(fc_mod.FirecrawlAPI, "scrape", create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_scrape))
    monkeypatch.setattr(InhouseFetchAdapter, "fetch", create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout))

    router = FetchRouter(["inhouse", "firecrawl"])
    fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")
    for i in range(5):
        await router.fetch(FetchRequest(urls=[f"https://target.example/{i}"]))
    assert fc_entry.breaker.is_open()

    skipped = await router.fetch(FetchRequest(urls=["https://target.example/skipped"]))
    assert skipped.results[0].error is not None
    assert skipped.results[0].error.type == WebErrorType.BUDGET_EXCEEDED

    # The recovery window elapses: the breaker lets traffic through half-open
    # and a request succeeds.
    fc_entry.breaker.last_failure_time = 1.0  # long ago (0.0 would read as unset)
    succeed["on"] = True
    ok = await router.fetch(FetchRequest(urls=["https://target.example/ok"]))
    assert ok.results[0].ok, ok.results[0].error
    assert "firecrawl" not in router._budget_errors


@pytest.mark.asyncio
async def test_batch_trip_keeps_the_monthly_notice_over_a_generic_402(monkeypatch: pytest.MonkeyPatch) -> None:
    """When one batch carries a generic 402 before a monthly-exhaustion 402, the
    stored notice is the one with the reset date."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("DALTON_ENABLED", "1")
    req = httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape")
    generic = httpx.Response(402, json={"success": False, "error": "Payment required"}, request=req)
    monthly = httpx.Response(
        402,
        json={
            "success": False,
            "code": "insufficient_credits",
            "error": "Insufficient credits",
            "remaining_credits": 0,
            "resets_at": "2026-11-26T00:00:00Z",
        },
        request=req,
    )

    async def _scrape(self, url, proxy=None, max_age_seconds=None):
        resp = generic if url.endswith("/a") else monthly
        raise httpx.HTTPStatusError("Firecrawl API error 402", request=req, response=resp)

    async def _inhouse_timeout(self, r, native_params):
        return FetchResponse(
            results=[FetchResult(url=u, error=WebError(type=WebErrorType.TIMEOUT, message="timed out")) for u in r.urls],
            provider="inhouse",
        )

    monkeypatch.setattr(fc_mod.FirecrawlAPI, "scrape", create_autospec(fc_mod.FirecrawlAPI.scrape, side_effect=_scrape))
    monkeypatch.setattr(InhouseFetchAdapter, "fetch", create_autospec(InhouseFetchAdapter.fetch, side_effect=_inhouse_timeout))

    router = FetchRouter(["inhouse", "firecrawl"])
    fc_entry = next(e for e in router._chain if e.adapter.name == "firecrawl")
    for i in range(5):
        await router.fetch(FetchRequest(urls=[f"https://t.example/{i}/a", f"https://t.example/{i}/b"]))
        if fc_entry.breaker.is_open():
            break
    assert fc_entry.breaker.is_open()
    skipped = await router.fetch(FetchRequest(urls=["https://t.example/later"]))
    assert skipped.results[0].error is not None
    assert "2026-11-26T00:00:00Z" in skipped.results[0].error.message
