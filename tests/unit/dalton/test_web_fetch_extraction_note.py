"""A4 review: an extraction failure must never throw the fetched page away,
and an exception with an empty message (asyncio timeouts) must not IndexError."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.tools.web import fetch as fetch_mod
from src.tools.web.fetch import _short_exc, web_fetch


def test_short_exc_uses_type_name_for_empty_messages() -> None:
    assert _short_exc(TimeoutError()) == "TimeoutError"
    assert _short_exc(asyncio.TimeoutError()) == "TimeoutError"
    assert _short_exc(ValueError("line one\nline two")) == "line one"
    long = ValueError("x" * 300)
    assert len(_short_exc(long)) == 120


@pytest.mark.asyncio
async def test_web_fetch_returns_page_when_extraction_times_out_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutError with an empty message: the page body comes back with the
    未能抽取（TimeoutError） marker — not 'list index out of range'."""
    page_markdown = "# 禾赛科技财报\n\n2024 财年 Revenue 为 $1,234 million。" + "正文 " * 60
    result = SimpleNamespace(
        url="https://example.com/report",
        title="Report",
        source="live",
        error=None,
        excerpts=None,
        summary=None,
        markdown=page_markdown,
        parse_timeout=False,
    )
    response = SimpleNamespace(
        provider="inhouse",
        provider_names=["inhouse"],
        attempts=[SimpleNamespace(str=lambda: "inhouse")],
        results=[result],
    )
    async def _fetch(request: Any) -> Any:
        return response

    service = SimpleNamespace(
        provider_names=["inhouse"],
        fetch=create_autospec(_fetch, return_value=response),
    )
    monkeypatch.setattr(fetch_mod, "get_fetch_service", lambda: service)

    async def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError()  # str() == ""

    monkeypatch.setattr(
        fetch_mod,
        "_extract_with_llm",
        create_autospec(fetch_mod._extract_with_llm, side_effect=_timeout),
    )

    content, artifact = await web_fetch(
        url="https://example.com/report", prompt="提取营收", use_cache=False, include_sitemap=False
    )
    assert content.startswith("未能抽取（TimeoutError）")
    assert page_markdown in content  # the page itself survives
    assert artifact["error"] == "extraction_failed"


@pytest.mark.asyncio
async def test_pdf_parse_timeout_wording_survives_the_empty_page_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PDF timeout wording is ~30 chars — under web_fetch's 50-char
    empty-page threshold. The parse_timeout flag must carry it through that
    gate verbatim, with its own error kind, so the model sees what actually
    happened."""
    from src.tools.web.inhouse.extractors.pdf import _PDF_PARSE_TIMEOUT_MESSAGE

    result = SimpleNamespace(
        url="https://example.com/huge.pdf",
        title="huge",
        source="live",
        error=None,
        excerpts=None,
        summary=None,
        markdown=_PDF_PARSE_TIMEOUT_MESSAGE,
        parse_timeout=True,
    )
    response = SimpleNamespace(
        provider="inhouse",
        provider_names=["inhouse"],
        attempts=[SimpleNamespace(str=lambda: "inhouse")],
        results=[result],
    )

    async def _fetch(request: Any) -> Any:
        return response

    service = SimpleNamespace(
        provider_names=["inhouse"],
        fetch=create_autospec(_fetch, return_value=response),
    )
    monkeypatch.setattr(fetch_mod, "get_fetch_service", lambda: service)

    content, artifact = await web_fetch(
        url="https://example.com/huge.pdf",
        prompt="提取营收",
        use_cache=False,
        include_sitemap=False,
    )
    assert content == _PDF_PARSE_TIMEOUT_MESSAGE  # verbatim, not "empty page"
    assert "empty" not in content.lower() or "超时" in content
    assert artifact["error"] == "pdf_parse_timeout"


@pytest.mark.asyncio
async def test_pdf_parse_timeout_notice_is_never_cached(monkeypatch) -> None:
    """DALTON-HOOK(heavy-compute-isolation): the cache keeps markdown only, so
    a cached timeout notice would come back without its flag and read as an
    empty page; the timeout result stays out of the cache, a real page goes in."""
    from src.tools.web.router import FetchRouter
    from src.tools.web.types import FetchRequest, FetchResponse, FetchResult
    from src.utils.cache.redis_cache import RedisCacheClient

    timed_out = FetchResult(
        url="https://example.com/a.pdf", markdown="PDF 解析超时", parse_timeout=True
    )
    page = FetchResult(url="https://example.com/b", markdown="x" * 200)
    router = create_autospec(FetchRouter, instance=True)
    router.fetch.return_value = FetchResponse(results=[timed_out, page])
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.get.return_value = None

    async def _cache_client():
        return cache

    monkeypatch.setattr(fetch_mod, "_get_cache_client", _cache_client)
    service = fetch_mod.FetchService.__new__(fetch_mod.FetchService)
    service._router = router

    await service.fetch(FetchRequest(urls=[timed_out.url, page.url]))

    cached_keys = [c.args[0] for c in cache.set.call_args_list]
    assert cached_keys == [fetch_mod._cache_key(page.url)]
