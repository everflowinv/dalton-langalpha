"""Web search through the Gemini API with Google Search grounding.

The request matches the OpenClaw gateway's Gemini path: ``generateContent``
on ``gemini-2.5-flash`` (overridable), ``tools: [{"google_search": {}}]``,
and the API key only in the ``x-goog-api-key`` header. Date bounds are a
sentence in the query, the same soft constraint the gateway used, because
grounding has no date parameter. ``count`` is clamped to 1–10 and the
citation list is cut to that size.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union

import httpx
from langchain_core.tools import tool

from src.dalton.web_search.cache import get_cached_search, search_cache_key, store_cached_search
from src.dalton.web_search.provider import (
    EMPTY_GENERATION_MESSAGE,
    OpenClawSearchEmptyGenerationError,
    OpenClawSearchEmptyResultError,
    OpenClawSearchRequestError,
    _answer_with_sources,
    _resolve_date_window,
    map_broker_response,
)
from src.dalton.web_search.redirects import resolve_grounding_redirects

logger = logging.getLogger(__name__)

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_SEARCH_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_SECONDS = 60.0
SEARCH_ENGINE = "gemini_grounding"
PROVIDER_NAME = "gemini"

ANSWER_ONLY_HEADING = "综合答案（无来源链接）"
ANSWER_WITH_SOURCES_HEADING = "综合答案（来源见下）"
RATE_LIMITED_MESSAGE = "网页搜索触发限流，请稍后重试"
UNAVAILABLE_MESSAGE = "网页搜索暂不可用"

_MODEL_RE_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class GroundingUnavailable(Exception):
    """The Gemini call cannot be completed. The message is safe to show."""


def configured_gemini_search_model() -> str:
    """``web_search.model`` from agent config, or ``gemini-2.5-flash``."""
    try:
        from src.config.tool_settings import _get_tool_config

        raw = _get_tool_config("web_search.model", DEFAULT_GEMINI_SEARCH_MODEL)
    except Exception:
        return DEFAULT_GEMINI_SEARCH_MODEL
    model = str(raw or "").strip()
    return model or DEFAULT_GEMINI_SEARCH_MODEL


def clamp_result_count(max_results: int) -> int:
    try:
        count = int(max_results)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(count, 10))


def augment_query(
    query: str,
    *,
    count: int,
    date_after: Optional[str],
    date_before: Optional[str],
) -> str:
    """The query plus the gateway's soft date preference and a source cap."""
    notes: List[str] = []
    if date_after or date_before:
        start = date_after or "any time"
        end = date_before or "any time"
        notes.append(
            "Prefer sources published from "
            f"{start} through {end}. This is a preference, not a hard cutoff."
        )
    notes.append(f"Use at most {count} sources.")
    return f"{query.strip()}\n\n" + "\n".join(notes)


def _candidate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping):
        return candidates[0]
    return {}


def candidate_text(payload: Mapping[str, Any]) -> str:
    content = _candidate(payload).get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return "\n\n".join(chunks)


def _error_status_name(body: Any) -> str:
    if not isinstance(body, Mapping):
        return ""
    err = body.get("error")
    if isinstance(err, Mapping):
        return str(err.get("status") or "").strip()
    if isinstance(err, str):
        return err.strip()
    return ""


def ordered_citations(metadata: Mapping[str, Any], *, limit: int) -> List[Dict[str, str]]:
    """``groundingChunks[].web`` in ``groundingSupports`` order, then the rest.

    A chunk index is kept once. Chunks no support cites keep their original
    order after the cited ones. The list is cut to ``limit``.
    """
    chunks = metadata.get("groundingChunks")
    if not isinstance(chunks, list):
        chunks = []
    supports = metadata.get("groundingSupports")
    if not isinstance(supports, list):
        supports = []

    order: List[int] = []
    seen: set[int] = set()

    def add(index: Any) -> None:
        if isinstance(index, bool) or not isinstance(index, int):
            return
        if index < 0 or index >= len(chunks) or index in seen:
            return
        seen.add(index)
        order.append(index)

    for support in supports:
        if not isinstance(support, Mapping):
            continue
        indices = support.get("groundingChunkIndices")
        if isinstance(indices, list):
            for index in indices:
                add(index)
    for index in range(len(chunks)):
        add(index)

    citations: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    for index in order:
        chunk = chunks[index]
        web = chunk.get("web") if isinstance(chunk, Mapping) else None
        if not isinstance(web, Mapping):
            continue
        url = str(web.get("uri") or "").strip()
        title = str(web.get("title") or "").strip()
        if not url and not title:
            continue
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        citations.append({"url": url, "title": title})
        if len(citations) >= limit:
            break
    return citations


def to_broker_payload(
    payload: Mapping[str, Any],
    *,
    query: str,
    limit: int,
) -> Dict[str, Any]:
    """Turn one ``generateContent`` body into the shape ``map_broker_response`` reads.

    No grounding metadata and no text is an empty generation (the caller
    retries once). Metadata with nothing citable and no text falls through
    to the mapper's empty-result error. Cards come from the grounding chunks.
    """
    text = candidate_text(payload)
    meta = _candidate(payload).get("groundingMetadata")
    has_meta = isinstance(meta, Mapping)
    if not has_meta and not text:
        raise OpenClawSearchEmptyGenerationError(EMPTY_GENERATION_MESSAGE)

    citations = ordered_citations(meta, limit=limit) if has_meta else []
    inner: Dict[str, Any] = {
        "provider": PROVIDER_NAME,
        "query": query,
        "answer": text,
        "citations": citations,
    }
    envelope: Dict[str, Any] = {
        "ok": True,
        "provider": PROVIDER_NAME,
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
    }
    response_id = payload.get("responseId")
    if isinstance(response_id, str) and response_id.strip():
        envelope["providerRequestId"] = response_id.strip()
    return envelope


def _raise_for_status(status: int, body: Any) -> None:
    status_name = _error_status_name(body)
    if status == 429 or status_name == "RESOURCE_EXHAUSTED":
        raise OpenClawSearchRequestError(RATE_LIMITED_MESSAGE, code="PROVIDER_RATE_LIMITED")
    if status == 400:
        raise OpenClawSearchRequestError(
            "Search failed: 参数错误 (HTTP 400)",
            code="INVALID_REQUEST",
        )
    raise GroundingUnavailable(f"{UNAVAILABLE_MESSAGE} (HTTP {status})")


class GeminiGroundingClient:
    """One ``generateContent`` call with Google Search grounding."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if api_key is None:
            api_key = os.environ.get("GEMINI_API_KEY", "")
        self._api_key = str(api_key or "")
        chosen = (model or configured_gemini_search_model()).strip()
        self.model = chosen or DEFAULT_GEMINI_SEARCH_MODEL
        self._timeout = (
            float(timeout_seconds) if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
        )
        self._transport = transport

    def _endpoint(self) -> str:
        if not self.model or any(ch not in _MODEL_RE_OK for ch in self.model):
            raise OpenClawSearchRequestError(
                "Search failed: 参数错误 (model)",
                code="INVALID_REQUEST",
            )
        return f"{GEMINI_API_ROOT}/models/{self.model}:generateContent"

    async def raw_results(
        self,
        query: str,
        max_results: int = 5,
        date_after: Optional[str] = None,
        date_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST one grounded generation and return a broker-shaped payload."""
        if not self._api_key.strip():
            raise GroundingUnavailable(UNAVAILABLE_MESSAGE)
        count = clamp_result_count(max_results)
        sent = augment_query(
            query, count=count, date_after=date_after, date_before=date_before
        )
        url = self._endpoint()
        body = {
            "contents": [{"role": "user", "parts": [{"text": sent}]}],
            "tools": [{"google_search": {}}],
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        status = 0
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(url, headers=headers, json=body)
            status = response.status_code
            try:
                parsed: Any = response.json()
            except Exception:
                parsed = None
            if status >= 400:
                _raise_for_status(status, parsed)
            if not isinstance(parsed, Mapping):
                raise GroundingUnavailable(UNAVAILABLE_MESSAGE)
            return to_broker_payload(parsed, query=query, limit=count)
        except (OpenClawSearchRequestError, OpenClawSearchEmptyGenerationError, GroundingUnavailable):
            raise
        except httpx.HTTPError:
            raise GroundingUnavailable(UNAVAILABLE_MESSAGE) from None
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "web search provider=%s model=%s status=%s elapsed_ms=%s",
                PROVIDER_NAME,
                self.model,
                status or "-",
                elapsed_ms,
            )


def build_web_search_tool(
    max_results: int = 10,
    default_time_range: Optional[str] = None,
    verbose: bool = True,
    include_favicon: bool = True,
    client: Optional[GeminiGroundingClient] = None,
    redirect_transport: Any = None,
    **_unused_kwargs: Any,
):
    """Build a per-request Gemini grounding ``web_search`` tool."""
    count = clamp_result_count(max_results)

    def _client() -> GeminiGroundingClient:
        if client is not None:
            return client
        return GeminiGroundingClient()

    @tool(response_format="content_and_artifact")
    async def web_search(
        query: str,
        time_range: Optional[Literal["day", "week", "month", "year", "d", "w", "m", "y"]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        topic: Optional[Literal["general", "news", "finance"]] = "general",
    ) -> Tuple[Union[List[Dict[str, Any]], str], Dict[str, Any]]:
        """Search the web for current information, news, and facts.

        Use when you need to:
        - Find recent news or current events
        - Look up facts, statistics, or real-time data
        - Research topics beyond your knowledge cutoff
        - Verify or update information

        Args:
            query: Search query to look up. Filter by time with `start_date`/`end_date` rather than putting dates in `query`.
            time_range: Filter by recency - 'day'/'d' (24h), 'week'/'w' (7d),
                'month'/'m' (30d), 'year'/'y' (365d).
                Ignored if start_date or end_date is provided.
            start_date: Start date for results (YYYY-MM-DD format).
                Takes priority over time_range.
            end_date: End date for results (YYYY-MM-DD format).
                Takes priority over time_range.
            topic: Search topic - 'general' (default), 'news', or 'finance'
                （该搜索不区分 topic，忽略该参数）

        Returns:
            Ranked results as titles, snippets and URLs — not full page text.
        """
        _ = (verbose, topic)
        try:
            date_after, date_before = _resolve_date_window(
                time_range=time_range,
                default_time_range=default_time_range,
                start_date=start_date,
                end_date=end_date,
                provider="gemini",
            )
            api = _client()
            cache_key = search_cache_key(
                SEARCH_ENGINE,
                {
                    "model": api.model,
                    "query": query,
                    "count": count,
                    "date_after": date_after,
                    "date_before": date_before,
                    "include_favicon": include_favicon,
                },
            )
            cached = await get_cached_search(cache_key)
            if cached is not None:
                logger.info(
                    "web search provider=%s model=%s status=hit elapsed_ms=0",
                    PROVIDER_NAME,
                    api.model,
                )
                return cached

            async def _once() -> Dict[str, Any]:
                return await api.raw_results(
                    query=query,
                    max_results=count,
                    date_after=date_after,
                    date_before=date_before,
                )

            started = time.monotonic()
            try:
                raw = await _once()
            except OpenClawSearchEmptyGenerationError:
                logger.warning("gemini grounding empty generation; retrying once")
                raw = await _once()
            elapsed = time.monotonic() - started
            cleaned, artifact = map_broker_response(
                raw,
                query=query,
                response_time=elapsed,
                include_favicon=include_favicon,
            )
            artifact["search_engine"] = SEARCH_ENGINE
            artifact["provider"] = PROVIDER_NAME
            await resolve_grounding_redirects(
                cleaned,
                artifact.get("results") or [],
                transport=redirect_transport,
            )
            if artifact.get("answer"):
                content: Union[List[Dict[str, Any]], str] = _answer_with_sources(
                    artifact["answer"],
                    cleaned,
                    only_heading=ANSWER_ONLY_HEADING,
                    with_sources_heading=ANSWER_WITH_SOURCES_HEADING,
                )
            else:
                content = cleaned
            await store_cached_search(cache_key, content, artifact)
            return content, artifact
        except OpenClawSearchEmptyResultError as exc:
            error_message = str(exc)
            logger.warning("gemini grounding returned empty results")
            return error_message, {"error": error_message, "query": query}
        except OpenClawSearchEmptyGenerationError as exc:
            error_message = str(exc)
            logger.warning("gemini grounding empty generation after retry")
            return error_message, {
                "error": error_message,
                "code": "empty_generation",
                "query": query,
            }
        except OpenClawSearchRequestError as exc:
            error_message = str(exc)
            logger.warning(
                "web search provider=%s model=%s status=%s elapsed_ms=0",
                PROVIDER_NAME,
                "-",
                exc.code,
            )
            return error_message, {"error": error_message, "code": exc.code, "query": query}
        except GroundingUnavailable as exc:
            error_message = str(exc) or UNAVAILABLE_MESSAGE
            logger.error(
                "web search provider=%s model=%s status=unavailable elapsed_ms=0",
                PROVIDER_NAME,
                "-",
            )
            return error_message, {"error": error_message, "query": query}
        except ValueError as exc:
            error_message = f"Search failed: 参数错误 ({exc})"
            logger.warning("gemini grounding parameter error")
            return error_message, {"error": error_message, "query": query}
        except Exception as exc:
            logger.error("gemini grounding failed: %s", type(exc).__name__)
            return UNAVAILABLE_MESSAGE, {"error": UNAVAILABLE_MESSAGE, "query": query}

    return web_search
