"""OpenClaw web search provider for Dalton (`search_api: openclaw`).

Routes public web search calls from the Dalton backend container to the
host-side HTTP bridge (`scripts/dalton/web_search_bridge.py` on `127.0.0.1:8961`),
which signs and forwards each query to the OpenClaw Unix-socket broker
(`~/.openclaw/dalton-web-search-broker.sock`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from src.dalton.web_search_spec import register_openclaw_web_provider  # noqa: F401  (re-export)
from src.dalton.settings import (
    get_dalton_web_search_bridge_queue_timeout_seconds,
    get_dalton_web_search_bridge_token,
    get_dalton_web_search_bridge_url,
)
from src.dalton.web_search.broker_protocol import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_MS,
    HTTP_TIMEOUT_MARGIN_SECONDS,
    compute_http_timeout_seconds,
)
from src.tools.utils.validation_utils import validate_date_format
from src.tools.web.providers._shared import (
    SNIPPET_MAX,
    clip_error,
    error_from_httpx,
    lazy,
    normalize_time_range,
    request_json,
    result_card,
    time_range_to_start_date,
)

logger = logging.getLogger(__name__)

BRIDGE_UNAVAILABLE_MESSAGE = "OpenClaw 网页搜索桥不可用"
BRIDGE_BUSY_MESSAGE = "OpenClaw 网页搜索繁忙，请稍后重试"
RATE_LIMITED_MESSAGE = "OpenClaw 网页搜索触发限流，请稍后重试"
EMPTY_RESULTS_MESSAGE = "OpenClaw 网页搜索返回空结果"
ANSWER_ONLY_HEADING = "OpenClaw 综合答案（无来源链接）"
ANSWER_WITH_SOURCES_HEADING = "OpenClaw 综合答案（来源见下）"

_PARAM_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "MISSING_FIELD",
        "UNKNOWN_FIELD",
        "INVALID_JSON",
        "DUPLICATE_KEY",
        "FRAME_TOO_LARGE",
    }
)
_RATE_LIMIT_CODES = frozenset(
    {
        "PROVIDER_RATE_LIMITED",
        "RATE_LIMITED",
    }
)

_UNTRUSTED_WRAPPER_RE = re.compile(
    r"^\s*<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>\s*(?:Source:[^\n]*\n---\n)?(.*?)\s*<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>\s*$",
    re.DOTALL,
)


class OpenClawSearchBridgeUnavailableError(RuntimeError):
    """Raised when the host-side OpenClaw web search bridge or broker cannot be reached."""

    def __init__(self, detail: str = "") -> None:
        cleaned = detail.strip()
        if cleaned.startswith(BRIDGE_UNAVAILABLE_MESSAGE):
            msg = cleaned
        elif cleaned:
            msg = f"{BRIDGE_UNAVAILABLE_MESSAGE}: {cleaned}"
        else:
            msg = BRIDGE_UNAVAILABLE_MESSAGE
        super().__init__(msg)


class OpenClawSearchRequestError(RuntimeError):
    """Raised when the bridge returns a structured 4xx/5xx error (busy, invalid params, rate limit)."""

    def __init__(self, message: str, *, code: str = "REQUEST_ERROR") -> None:
        super().__init__(message)
        self.code = code


class OpenClawSearchEmptyResultError(RuntimeError):
    """Raised when the broker returns ok=True with neither results/citations nor an answer."""

    def __init__(self, message: str = EMPTY_RESULTS_MESSAGE) -> None:
        super().__init__(message or EMPTY_RESULTS_MESSAGE)


def _answer_with_sources(answer: str, results: List[Dict[str, Any]]) -> Union[str, List[Dict[str, Any]]]:
    """The model-visible content when the broker returns a synthesized answer.

    Grounded providers (Gemini) return the answer plus citation links that carry no
    snippet, so the result list alone would show the model only domain names. The
    answer leads the list as an ``answer_box`` item: the UI's search cards and
    citation hovers keep reading the same JSON list and skip non page/news items.
    """
    if not results:
        return f"{ANSWER_ONLY_HEADING}\n{answer}"
    return [{"type": "answer_box", "title": ANSWER_WITH_SOURCES_HEADING, "content": answer}, *results]


def unwrap_untrusted_text(value: Any) -> str:
    """Strip OpenClaw's ``<<<EXTERNAL_UNTRUSTED_CONTENT>>>`` wrapper when present."""
    if not isinstance(value, str):
        return ""
    match = _UNTRUSTED_WRAPPER_RE.match(value)
    if match is not None:
        return match.group(1).strip()
    return value.strip()


def _favicon_url(url: str) -> str:
    """Build a Google favicon service URL from a page URL."""
    try:
        domain = urlparse(url).netloc.removeprefix("www.")
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=32" if domain else ""
    except Exception:
        return ""


def _resolve_date_window(
    *,
    time_range: Optional[str],
    default_time_range: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve tool time filters into the broker's ``(dateAfter, dateBefore)`` pair.

    The broker protocol requires ``dateAfter`` and ``dateBefore`` to be supplied
    together in ``YYYY-MM-DD`` format with ``dateAfter <= dateBefore``.
    """
    validate_date_format(start_date)
    validate_date_format(end_date)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if start_date or end_date:
        date_after = start_date or "1970-01-01"
        date_before = end_date or max(date_after, today_str)
        if date_after > date_before:
            raise ValueError("start_date must not be later than end_date")
        return date_after, date_before

    effective_range = normalize_time_range(
        time_range, default_time_range, provider="OpenClaw"
    )
    if not effective_range:
        return None, None

    computed_after = time_range_to_start_date(
        effective_range, fmt="%Y-%m-%d", provider="OpenClaw"
    )
    if not computed_after:
        return None, None
    return computed_after, max(computed_after, today_str)


def _raise_for_bridge_error_payload(
    payload: Mapping[str, Any],
    *,
    http_status: Optional[int] = None,
) -> None:
    """Classify a structured bridge/broker error payload and raise the appropriate exception."""
    err = payload.get("error")
    if isinstance(err, Mapping):
        code = str(err.get("code") or "").strip()
        raw_message = clip_error(str(err.get("message") or "").strip())
    else:
        code = ""
        raw_message = clip_error(str(err or "").strip())

    if code == "bridge_busy" or code.lower() == "bridge_busy":
        raise OpenClawSearchRequestError(BRIDGE_BUSY_MESSAGE, code="bridge_busy")

    if code in _RATE_LIMIT_CODES or http_status == 429:
        detail = f"{RATE_LIMITED_MESSAGE}: {raw_message}" if raw_message else RATE_LIMITED_MESSAGE
        raise OpenClawSearchRequestError(
            detail,
            code=code or "PROVIDER_RATE_LIMITED",
        )

    # A broker-side INVALID_JSON arrives as 5xx: that is the bridge failing, not a bad request.
    if http_status == 400 or (code in _PARAM_ERROR_CODES and http_status in (None, 400)):
        detail_parts = [p for p in (code, raw_message) if p]
        detail = ": ".join(detail_parts) if detail_parts else "invalid request"
        raise OpenClawSearchRequestError(
            f"Search failed: 参数错误 ({detail})",
            code=code or "INVALID_REQUEST",
        )

    if code and raw_message:
        raise OpenClawSearchBridgeUnavailableError(f"{code}: {raw_message}")
    raise OpenClawSearchBridgeUnavailableError(raw_message or code or "request failed")


def extract_broker_payload(broker_response: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap the inner provider payload from a broker reply frame."""
    if not isinstance(broker_response, Mapping):
        raise OpenClawSearchBridgeUnavailableError("Invalid response from bridge")

    if broker_response.get("ok") is False:
        _raise_for_bridge_error_payload(broker_response)

    result = broker_response.get("result")
    if isinstance(result, Mapping):
        content_blocks = result.get("content")
        if isinstance(content_blocks, list) and content_blocks:
            first_block = content_blocks[0]
            if isinstance(first_block, Mapping) and isinstance(first_block.get("text"), str):
                try:
                    parsed = json.loads(first_block["text"])
                except json.JSONDecodeError as exc:
                    raise OpenClawSearchBridgeUnavailableError(
                        "Broker tool-result text is not valid JSON"
                    ) from exc
                if isinstance(parsed, Mapping):
                    return dict(parsed)
        return dict(result)

    return dict(broker_response)


def map_broker_response(
    broker_response: Mapping[str, Any],
    *,
    query: str,
    response_time: float = 0.0,
    include_favicon: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Map an OpenClaw web search broker reply into ``(cleaned_results, artifact)``.

    Handles both list-style ``results`` payloads and OpenClaw's grounded
    ``answer`` + ``citations`` payloads (e.g. ``provider = "gemini"``).
    When individual results or citations have no snippet, ``content`` stays ``""``
    and the synthesized answer is placed only in ``artifact["answer"]``.
    When no valid results or citations exist but ``answer`` is present, ``cleaned_results``
    and ``artifact["results"]`` remain empty and the answer is stored only in ``artifact["answer"]``.
    Raises ``OpenClawSearchEmptyResultError`` when ``ok=True`` produces neither
    any valid result/citation nor an ``answer``.
    """
    payload = extract_broker_payload(broker_response)
    provider_name = str(
        payload.get("provider") or broker_response.get("provider") or "gemini"
    )
    request_id = broker_response.get("providerRequestId") or payload.get("requestId")
    answer_text = unwrap_untrusted_text(
        payload.get("content") or payload.get("answer") or ""
    )

    cleaned_results: List[Dict[str, Any]] = []
    cards: List[Dict[str, str]] = []

    raw_results = payload.get("results")
    raw_citations = payload.get("citations")

    if isinstance(raw_results, list) and raw_results:
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            url = unwrap_untrusted_text(str(item.get("url") or item.get("link") or ""))
            title = unwrap_untrusted_text(item.get("title") or item.get("name") or "")
            item_content = unwrap_untrusted_text(
                item.get("content")
                or item.get("snippet")
                or item.get("description")
                or ""
            )
            raw_content = unwrap_untrusted_text(item.get("raw_content") or "")
            if not (url or title or item_content or raw_content):
                continue
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            published_date = (
                item.get("published_date")
                or item.get("publishedDate")
                or item.get("published")
                or item.get("date")
            )
            favicon = (
                str(item.get("favicon") or "") or (_favicon_url(url) if include_favicon else "")
            ) if include_favicon else ""

            row: Dict[str, Any] = {
                "type": "page",
                "title": title,
                "url": url,
                "content": item_content,
                "score": score,
            }
            if published_date:
                row["date"] = str(published_date)
            if raw_content:
                row["raw_content"] = raw_content
            cleaned_results.append(row)
            cards.append(
                result_card(
                    title=title,
                    url=url,
                    favicon=favicon,
                    snippet=item_content[:SNIPPET_MAX],
                )
            )

    if not cleaned_results and isinstance(raw_citations, list) and raw_citations:
        for citation in raw_citations:
            if isinstance(citation, str):
                url = unwrap_untrusted_text(citation)
                title = ""
                item_content = ""
                published_date = None
                score = 0.0
                raw_favicon = ""
            elif isinstance(citation, Mapping):
                url = unwrap_untrusted_text(
                    str(citation.get("url") or citation.get("link") or "")
                )
                title = unwrap_untrusted_text(
                    citation.get("title") or citation.get("name") or ""
                )
                item_content = unwrap_untrusted_text(
                    citation.get("content")
                    or citation.get("snippet")
                    or citation.get("description")
                    or ""
                )
                published_date = (
                    citation.get("published_date")
                    or citation.get("publishedDate")
                    or citation.get("date")
                )
                try:
                    score = float(citation.get("score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                raw_favicon = str(citation.get("favicon") or "")
            else:
                continue

            if not (url or title or item_content):
                continue

            favicon = (
                raw_favicon or (_favicon_url(url) if include_favicon else "")
            ) if include_favicon else ""
            row = {
                "type": "page",
                "title": title,
                "url": url,
                "content": item_content,
                "score": score,
            }
            if published_date:
                row["date"] = str(published_date)
            cleaned_results.append(row)
            cards.append(
                result_card(
                    title=title,
                    url=url,
                    favicon=favicon,
                    snippet=item_content[:SNIPPET_MAX],
                )
            )

    if not cleaned_results and not answer_text:
        raise OpenClawSearchEmptyResultError(EMPTY_RESULTS_MESSAGE)

    artifact: Dict[str, Any] = {
        "type": "web_search",
        "query": query,
        "search_engine": "openclaw",
        "provider": provider_name,
        "response_time": round(response_time, 2),
        "total_results": len(cleaned_results),
        "results": cards,
    }
    if answer_text:
        artifact["answer"] = answer_text
    if isinstance(request_id, str) and request_id:
        artifact["request_id"] = request_id

    return cleaned_results, artifact


class OpenClawSearchWrapper:
    """HTTP client for the host-side OpenClaw web search bridge."""

    def __init__(
        self,
        *,
        bridge_url: Optional[str] = None,
        bridge_token: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        resolved_url = (bridge_url or get_dalton_web_search_bridge_url()).strip().rstrip("/")
        resolved_token = (
            bridge_token
            if bridge_token is not None
            else get_dalton_web_search_bridge_token()
        ).strip()
        if not resolved_token:
            raise OpenClawSearchBridgeUnavailableError(
                "DALTON_WEB_SEARCH_BRIDGE_TOKEN 未配置"
            )
        self._bridge_url = resolved_url
        self._bridge_token = resolved_token
        self._timeout_seconds: Optional[float] = (
            float(timeout_seconds) if timeout_seconds is not None else None
        )

    async def raw_results(
        self,
        query: str,
        max_results: int = 5,
        date_after: Optional[str] = None,
        date_before: Optional[str] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> Dict[str, Any]:
        """Send one search request to the host-side bridge and return the broker response."""
        payload: Dict[str, Any] = {
            "query": query,
            "count": max(1, min(int(max_results), 10)),
            "timeoutMs": int(timeout_ms),
        }
        if date_after is not None and date_before is not None:
            payload["dateAfter"] = date_after
            payload["dateBefore"] = date_before

        endpoint = (
            self._bridge_url
            if self._bridge_url.endswith("/search")
            else f"{self._bridge_url}/search"
        )
        queue_timeout_s = get_dalton_web_search_bridge_queue_timeout_seconds(
            DEFAULT_QUEUE_TIMEOUT_SECONDS
        )
        http_timeout = (
            self._timeout_seconds
            if self._timeout_seconds is not None
            else compute_http_timeout_seconds(
                timeout_ms,
                queue_timeout_seconds=queue_timeout_s,
            )
        )
        try:
            response = await request_json(
                "POST",
                endpoint,
                provider="OpenClawWebSearchBridge",
                headers={
                    "Authorization": f"Bearer {self._bridge_token}",
                    "Content-Type": "application/json",
                },
                json_body=payload,
                timeout=http_timeout,
            )
        except httpx.HTTPStatusError as exc:
            parsed_body: Any = None
            try:
                parsed_body = exc.response.json()
            except Exception:
                parsed_body = None
            if isinstance(parsed_body, Mapping) and "error" in parsed_body:
                _raise_for_bridge_error_payload(
                    parsed_body,
                    http_status=exc.response.status_code,
                )
            err = error_from_httpx(exc)
            raise OpenClawSearchBridgeUnavailableError(
                clip_error(err.message or str(exc))
            ) from exc
        except httpx.HTTPError as exc:
            err = error_from_httpx(exc)
            raise OpenClawSearchBridgeUnavailableError(
                clip_error(err.message or str(exc))
            ) from exc
        except OSError as exc:
            raise OpenClawSearchBridgeUnavailableError(clip_error(str(exc))) from exc

        if not isinstance(response, Mapping):
            raise OpenClawSearchBridgeUnavailableError("Invalid JSON response from bridge")
        if response.get("ok") is False:
            _raise_for_bridge_error_payload(response)
        return dict(response)

    async def search(
        self,
        query: str,
        max_results: int = 5,
        date_after: Optional[str] = None,
        date_before: Optional[str] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        include_favicon: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Execute search via the bridge and map the broker reply into tool output."""
        start_time = time.monotonic()
        raw = await self.raw_results(
            query=query,
            max_results=max_results,
            date_after=date_after,
            date_before=date_before,
            timeout_ms=timeout_ms,
        )
        elapsed = time.monotonic() - start_time
        return map_broker_response(
            raw,
            query=query,
            response_time=elapsed,
            include_favicon=include_favicon,
        )


def build_web_search_tool(
    max_results: int = 10,
    default_time_range: Optional[str] = None,
    verbose: bool = True,
    include_favicon: bool = True,
    wrapper: Optional[OpenClawSearchWrapper] = None,
    **_unused_kwargs: Any,
):
    """Build a per-request OpenClaw ``web_search`` tool for Dalton."""
    _get_api_wrapper = (
        (lambda: wrapper) if wrapper is not None else lazy(OpenClawSearchWrapper)
    )

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
                （OpenClaw 搜索不区分 topic，忽略该参数）

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
            )
            api = _get_api_wrapper()
            start_t = time.monotonic()
            raw = await api.raw_results(
                query=query,
                max_results=max_results,
                date_after=date_after,
                date_before=date_before,
            )
            elapsed = time.monotonic() - start_t
            cleaned_results, artifact = map_broker_response(
                raw,
                query=query,
                response_time=elapsed,
                include_favicon=include_favicon,
            )
            if artifact.get("answer"):
                return _answer_with_sources(artifact["answer"], cleaned_results), artifact
            return cleaned_results, artifact
        except OpenClawSearchEmptyResultError as exc:
            error_message = str(exc)
            logger.warning("OpenClaw web search returned empty results")
            return error_message, {"error": error_message, "query": query}
        except OpenClawSearchRequestError as exc:
            error_message = str(exc)
            logger.warning("OpenClaw web search request error: %s", exc.code)
            return error_message, {"error": error_message, "code": exc.code, "query": query}
        except OpenClawSearchBridgeUnavailableError as exc:
            error_message = str(exc)
            logger.error("OpenClaw web search bridge unavailable")
            return error_message, {"error": error_message, "query": query}
        except json.JSONDecodeError as exc:
            error_message = f"{BRIDGE_UNAVAILABLE_MESSAGE}: response is not JSON"
            logger.error("OpenClaw web search bridge returned non-JSON: %s", type(exc).__name__)
            return error_message, {"error": error_message, "query": query}
        except ValueError as exc:
            error_message = f"Search failed: 参数错误 ({exc})"
            logger.warning("OpenClaw web search parameter error: %s", exc)
            return error_message, {"error": error_message, "query": query}
        except (httpx.HTTPError, OSError) as exc:
            error_message = f"{BRIDGE_UNAVAILABLE_MESSAGE}: {clip_error(str(exc))}"
            logger.error("OpenClaw web search transport error: %s", type(exc).__name__)
            return error_message, {"error": error_message, "query": query}
        except Exception as exc:
            logger.error("OpenClaw web search failed: %s", type(exc).__name__)
            error_message = f"Search failed: {exc}"
            return error_message, {"error": str(exc), "query": query}

    return web_search


__all__ = [
    "ANSWER_ONLY_HEADING",
    "BRIDGE_BUSY_MESSAGE",
    "BRIDGE_UNAVAILABLE_MESSAGE",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_QUEUE_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_MS",
    "EMPTY_RESULTS_MESSAGE",
    "HTTP_TIMEOUT_MARGIN_SECONDS",
    "OpenClawSearchBridgeUnavailableError",
    "OpenClawSearchEmptyResultError",
    "OpenClawSearchRequestError",
    "OpenClawSearchWrapper",
    "RATE_LIMITED_MESSAGE",
    "build_web_search_tool",
    "compute_http_timeout_seconds",
    "extract_broker_payload",
    "map_broker_response",
    "register_openclaw_web_provider",
    "unwrap_untrusted_text",
]
