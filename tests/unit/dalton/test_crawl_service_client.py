"""Backend side of the crawl-service split (P6b): the opt-in/opt-out boundary,
the reconstructed page object, CrawlOutput parity with the in-process tier,
tier-3 request shape, and the no-fallback failure policy with its
``crawl_service_unavailable`` warning.

The service side is an ``httpx.MockTransport``; the serialized page payloads
come from the REAL worker serializer (``_serialize_page``), so the wire format
under test can never drift from what the service sends. The in-process leg of
each parity pair stubs ``_fetch_with_session`` (a real browser cannot run in
unit tests); the service leg is fully real down to the HTTP boundary.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

import src.config.tool_settings as tool_settings
from src.dalton.crawl_service.client import (
    CrawlServiceError,
    ServicePage,
    fetch_via_service,
)
from src.dalton.crawl_service.worker import _serialize_page
from src.tools.web.inhouse.safe_wrapper import SafeCrawlerWrapper
from src.tools.web.inhouse.scrapling_crawler import (
    ScraplingCrawler,
    _binary_payload,
    _extract_title,
)

GOOD_HTML = (
    "<html><head><title>Service Page</title></head><body>"
    + "<p>Content</p>" * 20
    + "</body></html>"
)
BLOCKED_HTML = "<html><body>Just a moment... Cloudflare Ray ID: abc</body></html>"
STEALTH_HTML = "<html><body>Cloudflare challenge Ray ID: xyz Just a moment</body></html>"
PDF_BODY = b"%PDF-1.4\n%" + b"x" * 400


class _FakePage:
    """The scrapling Response surface the crawler reads — the same facts the
    service serializes: body/encoding/headers/url/status + the css title."""

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        url: str = "https://ok.test/final",
        status: int = 200,
        title: str = "Service Page",
        encoding: str = "utf-8",
    ):
        self.body = body
        self.headers = {"content-type": content_type} if content_type else {}
        self.url = url
        self.status = status
        self.encoding = encoding
        self._title = title

    def css(self, selector):
        class _Node:
            def __init__(self, value):
                self._value = value

            def get(self):
                return self._value

        if selector == "title::text":
            return _Node(self._title)
        return _Node(None)


@pytest.fixture
def agent_config(monkeypatch: pytest.MonkeyPatch):
    holder = {"cfg": {}}
    monkeypatch.setattr(tool_settings, "_get_agent_config_dict", lambda: holder["cfg"])
    return holder


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Route the client's per-call AsyncClient into a MockTransport.

    Returns one ``{"body": <request JSON>, "token": <X-Service-Token>}`` entry
    per request.
    """
    captured: list[dict] = []

    def _wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "body": json.loads(request.content),
                "token": request.headers.get("X-Service-Token"),
            }
        )
        return handler(request)

    def _factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_wrapped_handler), timeout=timeout_s
        )

    monkeypatch.setattr("src.dalton.crawl_service.client._open_client", _factory)
    return captured


def _serve_page(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> list[dict]:
    payload = _serialize_page(page)
    return _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=payload)
    )


class TestOptOutBoundary:
    @pytest.mark.asyncio
    async def test_unset_url_keeps_in_process_path(self, agent_config, monkeypatch):
        crawler = ScraplingCrawler()
        page = _FakePage(GOOD_HTML.encode())
        opened = Mock(side_effect=AssertionError("service client must not open"))
        monkeypatch.setattr("src.dalton.crawl_service.client._open_client", opened)

        with patch.object(
            crawler,
            "_fetch_with_session",
            new_callable=AsyncMock,
            return_value=(page, GOOD_HTML, 200),
        ):
            got_page, html, status = await crawler._tier2_fetch("https://ok.test/x")

        assert (html, status) == (GOOD_HTML, 200)
        assert got_page is page
        opened.assert_not_called()


class TestServicePage:
    def test_headers_are_case_insensitive(self):
        page = ServicePage.from_payload(
            {
                "status": 200,
                "url": "u",
                "encoding": None,
                "headers": {"Content-Type": "application/pdf"},
                "body_b64": "",
                "title": "",
            }
        )
        assert page.headers.get("content-type") == "application/pdf"
        assert page.headers.get("CONTENT-TYPE") == "application/pdf"

    def test_css_serves_only_the_title_selector(self):
        page = ServicePage(
            status=200, url="u", encoding=None, headers={}, body=b"x", title="T"
        )
        assert page.css("title::text").get() == "T"
        assert page.css("a::attr(href)").get() is None

    def test_html_decode_uses_encoding(self):
        page = ServicePage(
            status=200,
            url="u",
            encoding="latin-1",
            headers={},
            body="résumé".encode("latin-1"),
            title="",
        )
        assert page.html == "résumé"

    def test_undecodable_body_raises(self):
        with pytest.raises(CrawlServiceError):
            ServicePage.from_payload({"body_b64": "abcde"})


class TestServiceRoundtripParity:
    """Same page, fetched "in-process" vs through the service: the triple the
    tier returns and the CrawlOutput the pipeline produces must match.

    codex r1 #5: the in-process leg runs with the service URL cleared — the
    patched ``_fetch_with_session`` must actually be awaited there, and the
    HTTP call count must not grow, or the two legs would both be service
    calls and the equality would prove nothing."""

    @pytest.mark.asyncio
    async def test_html_page_parity(self, agent_config, monkeypatch):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100/"}}
        fake = _FakePage(GOOD_HTML.encode())
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        crawler_service = ScraplingCrawler()
        crawler_inproc = ScraplingCrawler()
        captured = _serve_page(monkeypatch, fake)

        with (
            patch.object(
                crawler_service,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch.object(crawler_service, "_tier3_fetch", new_callable=AsyncMock) as t3,
            patch(
                "src.tools.web.inhouse.scrapling_crawler._extract_markdown",
                new_callable=AsyncMock,
                return_value="# md",
            ),
        ):
            out_service = await crawler_service.crawl_with_metadata("https://ok.test/x")

        # The in-process leg must run in-process: clear the URL the service
        # leg set, or this leg would silently hit MockTransport again.
        agent_config["cfg"] = {}
        local_fetch = AsyncMock(return_value=(fake, GOOD_HTML, 200))
        with (
            patch.object(
                crawler_inproc,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch.object(crawler_inproc, "_fetch_with_session", new=local_fetch),
            patch(
                "src.tools.web.inhouse.scrapling_crawler._extract_markdown",
                new_callable=AsyncMock,
                return_value="# md",
            ),
        ):
            out_inproc = await crawler_inproc.crawl_with_metadata("https://ok.test/x")

        t3.assert_not_awaited()
        assert local_fetch.await_count == 1, "in-process leg never used its own fetch"
        assert len(captured) == 1, "in-process leg must not add service calls"
        assert out_service == out_inproc
        assert out_service.title == "Service Page"
        assert out_service.status == 200
        assert out_service.failure_kind is None

    @pytest.mark.asyncio
    async def test_pdf_payload_parity(self, agent_config, monkeypatch):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        fake = _FakePage(
            PDF_BODY,
            content_type="application/pdf",
            url="https://files.test/report.pdf",
            title="",
        )
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        crawler_service = ScraplingCrawler()
        crawler_inproc = ScraplingCrawler()
        captured = _serve_page(monkeypatch, fake)

        with (
            patch.object(
                crawler_service,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch(
                "src.tools.web.inhouse.scrapling_crawler._extract_markdown",
                new_callable=AsyncMock,
            ),
        ):
            out_service = await crawler_service.crawl_with_metadata("https://files.test/report.pdf")

        agent_config["cfg"] = {}
        local_fetch = AsyncMock(return_value=(fake, "", 200))
        with (
            patch.object(
                crawler_inproc,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch.object(crawler_inproc, "_fetch_with_session", new=local_fetch),
            patch(
                "src.tools.web.inhouse.scrapling_crawler._extract_markdown",
                new_callable=AsyncMock,
            ),
        ):
            out_inproc = await crawler_inproc.crawl_with_metadata("https://files.test/report.pdf")

        assert local_fetch.await_count == 1, "in-process leg never used its own fetch"
        assert len(captured) == 1, "in-process leg must not add service calls"
        assert out_service == out_inproc
        assert out_service.binary_mime == "application/pdf"
        assert out_service.content_bytes == PDF_BODY
        assert out_service.final_url == "https://files.test/report.pdf"

    def test_binary_and_title_helpers_read_the_reconstructed_page(self):
        page = _FakePage(PDF_BODY, content_type="application/pdf")
        roundtripped = ServicePage.from_payload(_serialize_page(page))
        assert _binary_payload(roundtripped) == ("application/pdf", PDF_BODY)
        assert _extract_title(roundtripped) == "Service Page"


class TestTier3Delegation:
    @pytest.mark.asyncio
    async def test_solve_cloudflare_and_timeout_ride_the_request(
        self, agent_config, monkeypatch
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok-1")

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["tier"] == "browser":
                # Tier 2 sees a Cloudflare challenge → tier 3 runs the solver.
                return httpx.Response(
                    200, json=_serialize_page(_FakePage(STEALTH_HTML.encode(), title=""))
                )
            return httpx.Response(200, json=_serialize_page(_FakePage(GOOD_HTML.encode())))

        captured = _install_transport(monkeypatch, handler)
        crawler = ScraplingCrawler(timeout=45000)
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        with (
            patch.object(
                crawler,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch(
                "src.tools.web.inhouse.scrapling_crawler._extract_markdown",
                new_callable=AsyncMock,
                return_value="# md",
            ),
        ):
            out = await crawler.crawl_with_metadata("https://ok.test/x")

        assert [c["body"]["tier"] for c in captured] == ["browser", "stealth"]
        assert captured[1]["body"]["solve_cloudflare"] is True
        assert captured[0]["body"]["solve_cloudflare"] is False
        assert all(c["body"]["timeout_ms"] == 45000 for c in captured)
        assert out.status == 200
        assert out.title == "Service Page"


class TestNoFallbackPolicy:
    """Service configured + unavailable: the tier fails, the in-process
    session is never spawned, and the warning names tier/error/elapsed."""

    @pytest.mark.asyncio
    async def test_http_500_fails_tier_with_warning(
        self, agent_config, monkeypatch, caplog
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "unit-test-service-token")
        _install_transport(
            monkeypatch, lambda request: httpx.Response(500, text="boom")
        )
        crawler = ScraplingCrawler()
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        with (
            patch.object(
                crawler,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch.object(crawler, "_fetch_with_session", new_callable=AsyncMock) as local_fetch,
        ):
            with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
                with pytest.raises(CrawlServiceError):
                    await crawler.crawl_with_metadata("https://ok.test/x")

        local_fetch.assert_not_awaited()
        warnings = [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ]
        assert warnings, "expected crawl_service_unavailable warnings"
        payloads = [
            json.loads(w.getMessage().split("crawl_service_unavailable: ", 1)[1])
            for w in warnings
        ]
        # Tier 2 escalates to tier 3, which hits the same dead service.
        assert {p["tier"] for p in payloads} == {"browser", "stealth"}
        assert all(p["error_type"] == "http_500" for p in payloads)
        assert all("elapsed_ms" in p for p in payloads)
        assert all("unit-test-service-token" not in w.getMessage() for w in warnings)

    @pytest.mark.asyncio
    async def test_connection_failure_fails_tier_with_warning(
        self, agent_config, monkeypatch, caplog
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _install_transport(monkeypatch, handler)
        crawler = ScraplingCrawler()
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        with (
            patch.object(
                crawler,
                "_tier1_fetch",
                new_callable=AsyncMock,
                return_value=(t1, BLOCKED_HTML, 200),
            ),
            patch.object(crawler, "_fetch_with_session", new_callable=AsyncMock) as local_fetch,
        ):
            with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
                with pytest.raises(CrawlServiceError):
                    await crawler.crawl_with_metadata("https://ok.test/x")

        local_fetch.assert_not_awaited()
        warnings = [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ]
        assert warnings
        payloads = [
            json.loads(w.getMessage().split("crawl_service_unavailable: ", 1)[1])
            for w in warnings
        ]
        assert {p["error_type"] for p in payloads} == {"ConnectError"}


class TestFetchViaServiceClient:
    @pytest.mark.asyncio
    async def test_returns_page_html_status_with_auth_header(
        self, agent_config, monkeypatch
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100/"}}
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok-1")
        fake = _FakePage(GOOD_HTML.encode())
        captured = _serve_page(monkeypatch, fake)

        page, html, status = await fetch_via_service(
            "https://ok.test/x", tier="browser", timeout_ms=30000
        )

        assert isinstance(page, ServicePage)
        assert html == GOOD_HTML
        assert status == 200
        assert len(captured) == 1
        assert captured[0]["body"] == {
            "url": "https://ok.test/x",
            "tier": "browser",
            "solve_cloudflare": False,
            "timeout_ms": 30000,
        }
        assert captured[0]["token"] == "tok-1"


def _worker_failure_response(error_text: str) -> httpx.Response:
    """The service's structured 502 for a target-page fetch failure."""
    return httpx.Response(
        502,
        json={"detail": {"error_type": "worker_failure", "error": error_text}},
    )


class TestServiceFaultVsTargetFailure:
    """codex r1 #3 + agy r1 #3: a target page failing through a HEALTHY service
    is not a service-availability event — no ``crawl_service_unavailable``
    warning, full error text verbatim, ``crawl_service_fault=False`` — while
    connection failures, service 5xx and malformed replies are."""

    @pytest.mark.asyncio
    async def test_worker_failure_is_target_fault_with_full_text(
        self, agent_config, monkeypatch, caplog
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        # Long enough that a resp.text[:200] cut would lose the keyword.
        error_text = (
            "Playwright goto failed after retrying: "
            + "x" * 250
            + " net::ERR_NAME_NOT_RESOLVED at example.invalid"
        )
        _install_transport(monkeypatch, lambda request: _worker_failure_response(error_text))

        with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
            with pytest.raises(CrawlServiceError) as exc_info:
                await fetch_via_service("https://ok.test/x", tier="browser", timeout_ms=30000)

        err = exc_info.value
        assert err.crawl_service_fault is False
        assert err.error_type == "worker_failure"
        assert str(err) == error_text, "the browser's error text must arrive verbatim"
        assert not [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ], "a target-page failure is not a service-availability event"

    @pytest.mark.asyncio
    async def test_worker_timeout_is_target_fault_without_warning(
        self, agent_config, monkeypatch, caplog
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(504, json={"detail": "worker tree killed after 31.0s budget"}),
        )

        with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
            with pytest.raises(CrawlServiceError) as exc_info:
                await fetch_via_service("https://ok.test/x", tier="stealth", timeout_ms=30000)

        assert exc_info.value.crawl_service_fault is False
        assert exc_info.value.error_type == "worker_timeout"
        assert not [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ]

    @pytest.mark.asyncio
    async def test_worker_crash_503_is_service_fault_with_warning(
        self, agent_config, monkeypatch, caplog
    ):
        """r2 #4: an OOM-killed / crashed worker (no reply) is the service
        side's failure — warning on, infra class, never the page's."""
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                503,
                json={
                    "detail": {
                        "error_type": "worker_crash",
                        "error": "worker died without a usable reply (killed by signal 9)",
                    }
                },
            ),
        )

        with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
            with pytest.raises(CrawlServiceError) as exc_info:
                await fetch_via_service("https://ok.test/x", tier="browser", timeout_ms=30000)

        err = exc_info.value
        assert err.crawl_service_fault is True
        assert err.error_type == "worker_crash"
        assert "signal 9" in str(err)
        assert [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ], "a worker crash is a service-availability event"

    @pytest.mark.asyncio
    async def test_malformed_502_is_a_service_fault(
        self, agent_config, monkeypatch, caplog
    ):
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        _install_transport(
            monkeypatch, lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
        )

        with caplog.at_level("WARNING", logger="src.dalton.crawl_service.client"):
            with pytest.raises(CrawlServiceError) as exc_info:
                await fetch_via_service("https://ok.test/x", tier="browser", timeout_ms=30000)

        assert exc_info.value.crawl_service_fault is True
        assert exc_info.value.error_type == "http_502"
        assert [
            r for r in caplog.records if "crawl_service_unavailable" in r.getMessage()
        ], "a malformed 502 (no structured worker_failure) is the service's fault"


class TestWrapperBreakerClassification:
    """Through the real SafeCrawlerWrapper path: a service outage trips the
    global infra breaker and never the host's; a target-page failure reported
    by a healthy service classifies by keyword and charges the host only."""

    @pytest.mark.asyncio
    async def test_service_outage_charges_infra_not_host(
        self, agent_config, monkeypatch
    ):
        wrapper = SafeCrawlerWrapper()
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        crawler = ScraplingCrawler()
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        crawler._tier1_fetch = AsyncMock(return_value=(t1, BLOCKED_HTML, 200))
        wrapper._crawler = crawler

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _install_transport(monkeypatch, handler)

        result = await wrapper.crawl("https://ok.test/x")

        assert result.success is False
        assert result.error_type == "browser_closed"
        assert "Crawl service unavailable" in result.error
        assert wrapper._infra_breaker.failure_count == 1
        host_breaker = wrapper._host_breakers.get("ok.test")
        assert host_breaker is not None
        assert host_breaker.failure_count == 0, "service outage must not charge the host"

    @pytest.mark.asyncio
    async def test_worker_crash_charges_infra_not_host(
        self, agent_config, monkeypatch
    ):
        """r2 #4 through the real wrapper: an OOM-killed worker trips the
        infra breaker, never the target host's."""
        wrapper = SafeCrawlerWrapper()
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        crawler = ScraplingCrawler()
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        crawler._tier1_fetch = AsyncMock(return_value=(t1, BLOCKED_HTML, 200))
        wrapper._crawler = crawler

        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                503,
                json={
                    "detail": {
                        "error_type": "worker_crash",
                        "error": "worker died without a usable reply (killed by signal 9)",
                    }
                },
            ),
        )

        result = await wrapper.crawl("https://ok.test/x")

        assert result.success is False
        assert result.error_type == "browser_closed"
        assert "Crawl service unavailable" in result.error
        assert wrapper._infra_breaker.failure_count == 1
        host_breaker = wrapper._host_breakers.get("ok.test")
        assert host_breaker is not None
        assert host_breaker.failure_count == 0, "worker crash must not charge the host"

    @pytest.mark.asyncio
    async def test_worker_failure_classifies_by_keyword_and_charges_host(
        self, agent_config, monkeypatch
    ):
        wrapper = SafeCrawlerWrapper()
        agent_config["cfg"] = {"crawler": {"browser_service_url": "http://crawler:8100"}}
        crawler = ScraplingCrawler()
        t1 = _FakePage(BLOCKED_HTML.encode(), title="")
        crawler._tier1_fetch = AsyncMock(return_value=(t1, BLOCKED_HTML, 200))
        wrapper._crawler = crawler

        # Keyword sits past 200 chars: only the verbatim full text keeps the
        # classification identical to the in-process path.
        error_text = "x" * 250 + " net::ERR_NAME_NOT_RESOLVED at example.invalid"
        _install_transport(
            monkeypatch, lambda request: _worker_failure_response(error_text)
        )

        result = await wrapper.crawl("https://ok.test/x")

        assert result.success is False
        # Classified from the FULL verbatim text (keyword at offset 250); the
        # wrapper's display message keeps its own 200-char cut by design.
        assert result.error_type == "dns_error"
        host_breaker = wrapper._host_breakers.get("ok.test")
        assert host_breaker is not None
        assert host_breaker.failure_count == 1
        assert wrapper._infra_breaker.failure_count == 0, "target failure must not trip infra"
