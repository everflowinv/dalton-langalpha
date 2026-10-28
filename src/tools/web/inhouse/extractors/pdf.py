"""PDF content extractor using pdfplumber with pypdf fallback."""

import logging
import re
from urllib.parse import unquote, urlparse

from mcp_servers._pdf_parse import pdfplumber_extract, pypdf_extract  # DALTON-HOOK(heavy-compute-isolation)

from ..backend import CrawlOutput
from .base import ExtractorError, ContentExtractor, _validate_url, register_extractor
from src.dalton.heavy_compute import HeavyComputeError, HeavyComputeTimeout, run_in_heavy_subprocess

logger = logging.getLogger(__name__)

_MAX_PDF_SIZE = 20 * 1024 * 1024  # 20 MB

# Per-parser budget. The parse runs in a killable subprocess, so hitting it
# actually stops the work instead of abandoning a thread that keeps burning CPU.
_PDF_PARSE_TIMEOUT_S = 30

# DALTON-HOOK(heavy-compute-isolation): wording for a TERMINATED parse,
# distinct from "no text" (a scanned PDF) — see _PDF_PARSE_TIMEOUT_MESSAGE.
# What the model sees when the parse was TERMINATED at its budget — distinct
# from "no text" (a scanned PDF): the caller must not fall back to the HTML
# crawler on these bytes, and must not claim it looked like a scan.
_PDF_PARSE_TIMEOUT_MESSAGE = (
    "PDF 解析超时：解析被终止，文件可能过大或结构复杂，未获得正文。"
)


class PdfParseTimeout(Exception):
    """Both parsers were terminated at their budget; no text was produced."""


@register_extractor
class PdfExtractor(ContentExtractor):
    name = "pdf"
    url_patterns = [re.compile(r"\.pdf($|\?|#)", re.IGNORECASE)]

    async def extract(self, url: str) -> CrawlOutput | None:
        _validate_url(url)

        # Stream download with size guard
        content = await self._download(url)
        if content is None:
            return None

        # Extract text
        try:
            text = await self._extract_text(content)
        except PdfParseTimeout:
            # A terminated parse is NOT "extractor did not handle it": the
            # bytes are a PDF. Returning them to the content router would drop
            # them into the plain-web crawler, so answer in place.
            logger.warning(f"PDF parse terminated at budget for {url}")
            return CrawlOutput(
                title=self._title_from_url(url),
                html="",
                markdown=_PDF_PARSE_TIMEOUT_MESSAGE,
                parse_timeout=True,  # DALTON-HOOK(heavy-compute-isolation)
            )
        if text is None:
            return None

        title = self._title_from_url(url)
        return CrawlOutput(title=title, html="", markdown=text)

    # DALTON-HOOK(fetch-binary-route): entry for payloads the crawler already
    # downloaded through an extension-less redirect. Extraction failures are
    # stated in the output instead of returning None, because there is no
    # fallback left that could do better with the same bytes.
    async def extract_bytes(self, content: bytes, *, url: str) -> CrawlOutput:
        if len(content) > _MAX_PDF_SIZE:
            return CrawlOutput(
                title=self._title_from_url(url),
                html="",
                markdown=(
                    f"PDF 文本提取失败：文件超过 {_MAX_PDF_SIZE // (1024 * 1024)}MB 上限"
                    f"（{len(content)} 字节）；最终地址：{url}"
                ),
            )
        try:
            text = await self._extract_text(content)
        except PdfParseTimeout:
            return CrawlOutput(
                title=self._title_from_url(url),
                html="",
                markdown=_PDF_PARSE_TIMEOUT_MESSAGE,
                parse_timeout=True,  # DALTON-HOOK(heavy-compute-isolation)
            )
        if text is None or not text.strip():
            return CrawlOutput(
                title=self._title_from_url(url),
                html="",
                markdown=f"PDF 文本提取失败：pdfplumber 与 pypdf 均未提取出文本（可能是扫描件）；最终地址：{url}",
            )
        return CrawlOutput(title=self._title_from_url(url), html="", markdown=text)

    async def _download(self, url: str) -> bytes | None:
        """Download PDF with size guard. Returns None if too large or wrong type."""
        try:
            async with self._client.stream("GET", url) as resp:
                resp.raise_for_status()

                # Check Content-Type
                ct = resp.headers.get("content-type", "")
                if ct and "application/pdf" not in ct:
                    return None

                # Check Content-Length if available
                cl = resp.headers.get("content-length")
                if cl and int(cl) > _MAX_PDF_SIZE:
                    logger.warning(f"PDF too large ({cl} bytes): {url}")
                    return None

                # Read in chunks up to limit
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > _MAX_PDF_SIZE:
                        logger.warning(f"PDF exceeded {_MAX_PDF_SIZE} bytes during streaming: {url}")
                        return None
                    chunks.append(chunk)

                return b"".join(chunks)
        except ExtractorError:
            raise
        except Exception as e:
            raise ExtractorError(f"Failed to download PDF: {e}") from e

    async def _extract_text(self, content: bytes) -> str | None:
        """Extract text via pdfplumber, falling back to pypdf.

        DALTON-HOOK(heavy-compute-isolation): each parse runs in a bounded
        subprocess with a hard deadline. The old ``wait_for(to_thread(...))``
        only stopped *waiting* — the orphaned thread kept its GIL-holding parse
        alive, and the fallback parser then stacked a second one on top.
        Raises :class:`PdfParseTimeout` when the parsers were killed at budget,
        so callers can say THAT instead of guessing "scanned document".
        """
        timed_out = False
        for parse, parser_name in (
            (pdfplumber_extract, "pdfplumber"),
            (pypdf_extract, "pypdf"),
        ):
            try:
                text = await run_in_heavy_subprocess(
                    parse,
                    content,
                    timeout_s=_PDF_PARSE_TIMEOUT_S,
                    label=f"pdf-extract:{parser_name}",
                )
                if text and text.strip():
                    return text
            except HeavyComputeTimeout:
                logger.warning(
                    f"{parser_name} extraction terminated at {_PDF_PARSE_TIMEOUT_S}s budget"
                )
                timed_out = True
            except HeavyComputeError as e:
                logger.debug(f"{parser_name} parse failed, trying next parser: {e}")

        if timed_out:
            raise PdfParseTimeout(parser_name)
        return None

    @staticmethod
    def _title_from_url(url: str) -> str:  # DALTON-HOOK(heavy-compute-isolation)
        path = urlparse(url).path
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        name = re.sub(r"\.pdf$", "", unquote(filename), flags=re.IGNORECASE)
        return name or "PDF Document"
