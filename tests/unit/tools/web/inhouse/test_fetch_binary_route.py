"""fetch-binary-route: extension-less redirects that land on PDF/image payloads."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from src.tools.web.inhouse.backend import CrawlOutput
from src.tools.web.inhouse.content_router import ContentRouter
from src.tools.web.inhouse.extractors.pdf import PdfExtractor, _MAX_PDF_SIZE
from src.tools.web.inhouse.scrapling_crawler import _binary_payload


class _FakePage:
    """The scrapling Response surface `_binary_payload` reads: raw body + headers + url."""

    def __init__(self, body: bytes, content_type: str = "", url: str = "https://x/y"):
        self.body = body
        self.headers = {"content-type": content_type} if content_type else {}
        self.url = url
        self.status = 200


def _pdf_extractor_double(markdown: str = "PDF text") -> PdfExtractor:
    double = create_autospec(PdfExtractor, instance=True)
    double.name = "pdf"
    # The extension-less URL must not match the extractor's URL patterns; only
    # the binary-route dispatch below should reach this double.
    double.matches.return_value = False
    double.extract_bytes.return_value = CrawlOutput(
        title="doc", html="", markdown=markdown
    )
    return double


def _router_with(fallback_output: CrawlOutput, pdf_extractor: PdfExtractor | None):
    from unittest.mock import patch

    router = ContentRouter()
    fallback = create_autospec(router._fallback, instance=True)
    fallback.crawl_with_metadata.return_value = fallback_output
    fallback.shutdown.return_value = None
    router._fallback = fallback
    registry = {"pdf": pdf_extractor} if pdf_extractor is not None else {}
    return router, fallback, patch(
        "src.tools.web.inhouse.content_router.get_extractor_registry",
        return_value=registry,
    )


class TestBinaryPayloadSniff:
    def test_pdf_signature_beats_missing_content_type(self):
        mime, raw = _binary_payload(_FakePage(b"%PDF-1.7 ..."))
        assert mime == "application/pdf"
        assert raw.startswith(b"%PDF-")

    def test_jpeg_signature_detected(self):
        mime, _ = _binary_payload(_FakePage(b"\xff\xd8\xff\xe0\x00\x10JFIF"))
        assert mime == "image/jpeg"

    def test_png_and_gif_signatures_detected(self):
        assert _binary_payload(_FakePage(b"\x89PNG\r\n\x1a\n"))[0] == "image/png"
        assert _binary_payload(_FakePage(b"GIF89a"))[0] == "image/gif"

    def test_plain_html_page_is_not_binary(self):
        page = _FakePage(b"<!doctype html><html>" + b"x" * 400, "text/html; charset=utf-8")
        assert _binary_payload(page) is None

    def test_text_body_with_binary_content_type_is_not_binary(self):
        page = _FakePage(b"plain text body", "text/plain")
        assert _binary_payload(page) is None

    def test_image_content_type_without_signature_is_binary(self):
        page = _FakePage(b"\x00\x01\x02binaryish", "image/webp")
        mime, _ = _binary_payload(page)
        assert mime == "image/webp"


class TestRouterBinaryDispatch:
    @pytest.mark.asyncio
    async def test_pdf_bytes_route_to_pdf_extractor(self):
        """Redirect URL returning PDF bytes: bytes go to PdfExtractor, not the model."""
        pdf_double = _pdf_extractor_double()
        output = CrawlOutput(
            title="",
            html="",
            markdown="",
            status=200,
            binary_mime="application/pdf",
            content_bytes=b"%PDF-1.4 fake",
            final_url="https://final.example.com/report.pdf",
        )
        router, fallback, registry_patch = _router_with(output, pdf_double)
        with registry_patch:
            result = await router.crawl_with_metadata("https://example.com/redirect")

        assert result.markdown == "PDF text"
        pdf_double.extract_bytes.assert_awaited_once_with(
            b"%PDF-1.4 fake", url="https://final.example.com/report.pdf"
        )
        # No second fetch: the fallback ran exactly once for this URL.
        fallback.crawl_with_metadata.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jpeg_returns_short_notice_not_bytes(self):
        output = CrawlOutput(
            title="",
            html="",
            markdown="",
            status=200,
            binary_mime="image/jpeg",
            content_bytes=b"\xff\xd8\xff whatever",
            final_url="https://final.example.com/photo.jpg",
        )
        router, fallback, registry_patch = _router_with(output, None)
        with registry_patch:
            result = await router.crawl_with_metadata("https://example.com/redirect")

        assert "该链接是图片（image/jpeg）" in result.markdown
        assert "https://final.example.com/photo.jpg" in result.markdown
        assert "\xff\xd8" not in result.markdown.encode("utf-8", errors="ignore").decode(
            "latin-1", errors="ignore"
        )

    @pytest.mark.asyncio
    async def test_plain_html_page_costs_exactly_one_fallback_fetch(self):
        output = CrawlOutput(
            title="Example",
            html="<html><body>" + "content " * 50 + "</body></html>",
            markdown="Example content",
            status=200,
        )
        router, fallback, registry_patch = _router_with(output, None)
        with registry_patch:
            result = await router.crawl_with_metadata("https://example.com/page")

        assert result.markdown == "Example content"
        fallback.crawl_with_metadata.assert_awaited_once()
        assert fallback.crawl_with_metadata.await_count == 1


class TestPdfExtractBytesFailures:
    @pytest.mark.asyncio
    async def test_unparseable_bytes_report_extraction_failure(self):
        extractor = PdfExtractor()
        result = await extractor.extract_bytes(b"not actually a pdf", url="https://x/y")
        assert result.markdown.startswith("PDF 文本提取失败")
        assert "https://x/y" in result.markdown

    @pytest.mark.asyncio
    async def test_oversized_bytes_report_size_limit(self):
        extractor = PdfExtractor()
        result = await extractor.extract_bytes(
            b"%PDF-1.4" + b"0" * (_MAX_PDF_SIZE + 1), url="https://x/y"
        )
        assert result.markdown.startswith("PDF 文本提取失败")
        assert "20MB" in result.markdown
