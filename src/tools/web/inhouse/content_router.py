"""Content router — tries specialised extractors before falling back to ScraplingCrawler."""

import logging

from .backend import CrawlOutput
from .extractors import ExtractorError, get_extractor_registry
from .extractors.base import _validate_url
from .scrapling_crawler import ScraplingCrawler

logger = logging.getLogger(__name__)


class ContentRouter:
    """
    Routes URLs to specialised extractors, with ScraplingCrawler as fallback.

    Satisfies the CrawlerBackend protocol.
    """

    def __init__(
        self,
        http_concurrency: int = 20,
        browser_concurrency: int = 6,
    ) -> None:
        self._fallback = ScraplingCrawler(
            http_concurrency=http_concurrency,
            browser_concurrency=browser_concurrency,
        )

    async def crawl_with_metadata(self, url: str) -> CrawlOutput:
        # SSRF protection: block private IPs, localhost, non-http(s) schemes
        # for ALL URLs before any extractor or ScraplingCrawler processes them
        _validate_url(url)

        for extractor in get_extractor_registry().values():
            if not extractor.matches(url):
                continue

            try:
                result = await extractor.extract(url)
                if result is not None:
                    logger.debug(f"Extractor '{extractor.name}' handled {url}")
                    return result
                logger.debug(f"Extractor '{extractor.name}' returned None for {url}, falling through")
            except ExtractorError as e:
                logger.warning(f"Extractor '{extractor.name}' error for {url}: {e}")
            except Exception as e:
                logger.warning(f"Extractor '{extractor.name}' unexpected error for {url}: {e}")

        result = await self._fallback.crawl_with_metadata(url)
        # DALTON-HOOK(fetch-binary-route): the fallback's final response was a
        # PDF/image (extension-less redirect), not a page — dispatch the already
        # downloaded bytes instead of returning them decoded as text.
        if result.binary_mime:
            return await self._route_binary(result, url)
        return result

    async def _route_binary(self, result: CrawlOutput, url: str) -> CrawlOutput:
        mime = result.binary_mime or ""
        final_url = result.final_url or url
        if mime == "application/pdf":
            pdf_extractor = get_extractor_registry().get("pdf")
            if pdf_extractor is None:
                return CrawlOutput(
                    title="",
                    html="",
                    markdown=f"PDF 文本提取失败：PDF 提取器不可用；最终地址：{final_url}",
                )
            return await pdf_extractor.extract_bytes(
                result.content_bytes or b"", url=final_url
            )
        if mime.startswith("image/"):
            return CrawlOutput(
                title="",
                html="",
                markdown=f"该链接是图片（{mime}），无法作为网页文本读取；最终地址：{final_url}",
            )
        return CrawlOutput(
            title="",
            html="",
            markdown=f"该链接不是网页（{mime}），无法作为网页文本读取；最终地址：{final_url}",
        )

    async def crawl(self, url: str) -> str:
        output = await self.crawl_with_metadata(url)
        return output.markdown

    async def shutdown(self) -> None:
        for extractor in get_extractor_registry().values():
            try:
                await extractor.shutdown()
            except Exception as e:
                logger.debug(f"Error shutting down extractor '{extractor.name}': {e}")
        await self._fallback.shutdown()
