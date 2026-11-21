"""Helpers shared by the web provider adapters and search builders.

The raw-httpx providers repeat the same moves: issue a JSON API call and map
its HTTP failure to the normalized taxonomy, poll an async research job until
terminal, unwrap a research report, normalize a time-range filter, and
assemble per-URL fetch results back into request order. Provider quirks stay
local and come in as ``overrides`` / format strings. Frontend result-card and
snippet/error-truncation shapes are shared where identical.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
# DALTON-HOOK(firecrawl-budget): multi-key failover
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

import httpx

from src.observability import mark_tool_failed, mark_tool_unavailable
from src.tools.web.types import (
    FetchResponse,
    FetchResult,
    WebError,
    WebErrorType,
    WebToolError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Snippet/excerpt truncation for frontend result cards; error-message clipping.
SNIPPET_MAX = 300
ERROR_MESSAGE_MAX = 300


def clip_error(message: str) -> str:
    """Bound a provider error string before it enters a WebError message."""
    return message[:ERROR_MESSAGE_MAX]


def http_status_error(
    status: int, body: str, overrides: Optional[Mapping[int, "WebErrorType"]] = None
) -> WebError:
    """Map a provider-API HTTP failure to the taxonomy.

    401 means our key was rejected, 403 that or a site the provider refuses,
    402 that the provider account's budget is exhausted, 429 rate limiting;
    anything else is a generic provider error. ``overrides`` adds provider
    quirks (e.g. firecrawl 408 -> timeout).

    Always ``retryable``: this is a whole-call failure against the
    *provider's* endpoint (our key/account/quota), not a verdict on the
    URL — so the router advances to the next provider. A revoked primary
    key therefore degrades to the fallbacks instead of failing the fetch.
    Per-URL forbidden/paywall statuses are built elsewhere and stay terminal.
    """
    provider_fault = None
    if overrides and status in overrides:
        etype = overrides[status]
    elif status in (401, 403):
        etype = WebErrorType.FORBIDDEN
        # A 401 is our key, so the provider's side. A 403 can be the provider
        # refusing one site (Firecrawl's blocklist), which must not open the
        # breaker every other URL shares.
        if status == 401:
            provider_fault = True
    elif status == 402:
        etype = WebErrorType.BUDGET_EXCEEDED
        # Our account at the provider ran dry: someone has to top it up, and
        # every URL fails the same way until then, so the breaker counts it.
        provider_fault = True
    elif status == 429:
        etype = WebErrorType.RATE_LIMITED
    else:
        etype = WebErrorType.PROVIDER_ERROR
    return WebError(
        type=etype,
        message=clip_error(body),
        http_status=status,
        retryable=True,
        provider_fault=provider_fault,
    )


def transport_error(e: httpx.HTTPError) -> WebError:
    """Map an httpx transport failure; timeouts keep their taxonomy type."""
    if isinstance(e, httpx.TimeoutException):
        return WebError(type=WebErrorType.TIMEOUT, message=str(e) or "Request timed out")
    return WebError(type=WebErrorType.PROVIDER_ERROR, message=str(e))


def error_from_httpx(
    e: httpx.HTTPError, *, status_overrides: Optional[Mapping[int, "WebErrorType"]] = None
) -> WebError:
    """Normalize any httpx failure: status errors keep the response body and
    honor ``status_overrides``; other transport failures map by type."""
    if isinstance(e, httpx.HTTPStatusError):
        return http_status_error(e.response.status_code, e.response.text, status_overrides)
    return transport_error(e)


def _as_web_error(error: WebError | BaseException) -> Optional[WebError]:
    if isinstance(error, WebToolError):
        return error.error
    return error if isinstance(error, WebError) else None


def mark_web_outcome(
    error: WebError | BaseException,
    provider: Optional[str] = None,
    *,
    provider_side: bool = False,
) -> None:
    """Record a failed web call as the provider's failure or the target's refusal.

    Decided by the error's own ``provider_side``. A timeout is the target's
    there: a slow page and a slow provider look the same from here, so it
    goes as the breaker treats it. The ``provider_side`` argument is for a
    call the target plays no part in, where any failure is the provider's. An
    exception that is not a web error is our own pipeline failing.
    """
    web_error = _as_web_error(error)
    if web_error is None:
        mark_tool_failed(error, provider)
    elif provider_side or web_error.provider_side:
        mark_tool_failed(web_error.type.value, provider)
    else:
        mark_tool_unavailable(web_error.type.value, provider)


def search_refusal(error: BaseException, query: str) -> Tuple[str, Dict[str, Any]]:
    """The text and artifact a WebSearch returns in place of results."""
    return f"Search failed: {error}", {"error": str(error), "query": query}


def search_failed(
    error: BaseException, query: str, *, quote_http_body: bool = False
) -> Tuple[str, Dict[str, Any]]:
    """Mark a failed WebSearch and return its refusal.

    Search has no target site to blame, so a failure is the provider's (a
    rejected key and a timeout included) or ours. ``quote_http_body`` keeps
    the status-and-body refusal the raw-httpx tools return for an HTTP error.
    """
    mark_web_outcome(
        error_from_httpx(error) if isinstance(error, httpx.HTTPError) else error,
        provider_side=True,
    )
    if quote_http_body and isinstance(error, httpx.HTTPStatusError):
        response = error.response
        status = response.status_code if response is not None else "unknown"
        body = response.text if response is not None else "no response body"
        message = f"Search failed (HTTP {status}): {body}"
        return message, {"error": message, "query": query}
    return search_refusal(error, query)


def missing_key_error(message: str) -> WebError:
    """Adapter built without its API key — retryable so the chain moves on."""
    return WebError(type=WebErrorType.PROVIDER_ERROR, message=message, retryable=True)


_SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie"})


def strip_url_query(raw_url: Any) -> str:
    """Return ``raw_url`` without query parameters or fragments."""
    text = str(raw_url or "")
    if not text:
        return ""
    try:
        return str(httpx.URL(text).copy_with(query=None, fragment=None))
    except Exception:
        return text.split("?", 1)[0].split("#", 1)[0]


def scrub_text(text: str, scrub_values: Sequence[str] = ()) -> str:
    """Replace any raw secret strings in ``text`` with ``[REDACTED]``."""
    cleaned = text
    for secret in scrub_values:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


# DALTON-HOOK(firecrawl-budget): redact auth headers and query strings before attaching requests to exceptions.
def redact_request_headers(
    req: Optional[httpx.Request], *, strip_query: bool = True
) -> Optional[httpx.Request]:
    """Return a shallow copy of ``req`` with credential headers replaced by ``[REDACTED]``."""
    if req is None:
        return None
    clean_headers = httpx.Headers(
        {
            k: ("[REDACTED]" if k.lower() in _SENSITIVE_HEADERS else v)
            for k, v in req.headers.items()
        }
    )
    clean_url = strip_url_query(req.url) if strip_query else req.url
    return httpx.Request(req.method, clean_url, headers=clean_headers)


def _sanitized_response(
    resp: Optional[httpx.Response],
    clean_req: Optional[httpx.Request],
    scrub_values: Sequence[str] = (),
) -> Optional[httpx.Response]:
    if resp is None:
        return None
    try:
        raw_content = resp.content
        if scrub_values and raw_content:
            text_body = resp.text
            cleaned_text = scrub_text(text_body, scrub_values)
            if cleaned_text != text_body:
                raw_content = cleaned_text.encode("utf-8")
        safe_headers = [
            (k, v)
            for k, v in resp.headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(
            resp.status_code,
            content=raw_content,
            headers=safe_headers,
            request=clean_req,
        )
    except Exception:
        resp.request = clean_req
        if hasattr(resp, "history") and isinstance(resp.history, list):
            resp.history = []
        return resp


def sanitize_httpx_error(
    err: httpx.HTTPError,
    *,
    method: Optional[str] = None,
    url: Optional[str] = None,
    scrub_values: Sequence[str] = (),
) -> httpx.HTTPError:
    """Build a fresh httpx exception carrying only sanitized request/status details."""
    raw_req = getattr(err, "_request", None)
    if raw_req is None and isinstance(err, httpx.HTTPStatusError):
        raw_req = getattr(err.response, "request", None)
    clean_req = redact_request_headers(raw_req, strip_query=True)
    if clean_req is None and (method or url):
        clean_req = httpx.Request(method or "", strip_url_query(url))

    if isinstance(err, httpx.HTTPStatusError):
        clean_resp = _sanitized_response(err.response, clean_req, scrub_values=scrub_values)
        status = getattr(err.response, "status_code", None)
        exc_name = type(err).__name__
        clean_msg = scrub_text(str(err), scrub_values)
        if not clean_msg or any(
            marker in clean_msg.lower() for marker in ("authorization", "x-api-key")
        ):
            clean_url = strip_url_query(clean_req.url if clean_req else url)
            req_method = clean_req.method if clean_req else (method or "")
            clean_msg = f"{exc_name} {status}: {req_method} {clean_url}".strip()
        new_err: httpx.HTTPError = httpx.HTTPStatusError(
            clean_msg,
            request=clean_req or httpx.Request(method or "", strip_url_query(url)),
            response=clean_resp,
        )
        new_err.__cause__ = None
        new_err.__context__ = None
        return new_err

    exc_name = type(err).__name__
    req_method = clean_req.method if clean_req else (method or "")
    clean_url = strip_url_query(clean_req.url if clean_req else url)
    status = getattr(getattr(err, "response", None), "status_code", None)
    parts = [exc_name]
    if status is not None:
        parts.append(f"HTTP {status}")
    target = " ".join(p for p in (req_method, clean_url) if p)
    msg = f"{' '.join(parts)}: {target}" if target else " ".join(parts)
    msg = scrub_text(msg, scrub_values)

    if isinstance(err, httpx.RequestError):
        try:
            new_err = type(err)(msg, request=clean_req)
        except TypeError:
            fallback_cls = (
                httpx.TimeoutException
                if isinstance(err, httpx.TimeoutException)
                else httpx.RequestError
            )
            new_err = fallback_cls(msg, request=clean_req)
    else:
        try:
            new_err = type(err)(msg)
        except TypeError:
            new_err = httpx.HTTPError(msg)
    new_err.__cause__ = None
    new_err.__context__ = None
    return new_err


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Optional[Any] = None,
    params: Optional[Mapping[str, Any]] = None,
    timeout: float,
) -> Dict[str, Any]:
    """Issue one JSON API call and return the decoded body.

    A status >= 400 raises ``httpx.HTTPStatusError`` with the providers'
    shared ``"{Provider} API error {code}: {text}"`` message so callers can
    funnel it through ``error_from_httpx``; transport failures propagate as
    freshly constructed sanitized httpx exceptions without credentials,
    query parameters, or cause/context chains.
    """
    sanitized_err: Optional[httpx.HTTPError] = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
            # DALTON-HOOK(firecrawl-budget): multi-key failover
            if response.status_code >= 400:
                clean_req = redact_request_headers(response.request)
                clean_resp = _sanitized_response(response, clean_req)
                sanitized_err = httpx.HTTPStatusError(
                    f"{provider} API error {response.status_code}: {response.text}",
                    request=clean_req or httpx.Request(method, strip_url_query(url)),
                    response=clean_resp,
                )
            else:
                return response.json()
    except httpx.HTTPError as e:
        sanitized_err = sanitize_httpx_error(e, method=method, url=url)
    finally:
        headers = None
    raise sanitized_err from None


# Terminal states shared by the async research/task poll loops.
RESEARCH_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def poll_until_terminal(
    refresh: Callable[[], Awaitable[Tuple[str, Dict[str, Any]]]],
    *,
    status: str,
    data: Dict[str, Any],
    max_wait_seconds: float,
    poll_interval: float,
    provider: str,
) -> Tuple[str, Dict[str, Any]]:
    """Poll ``refresh`` until the run reaches a terminal state or the deadline.

    ``refresh`` returns the latest ``(status, data)``. Raises a TIMEOUT
    ``WebToolError`` once ``max_wait_seconds`` elapses; ``provider`` is the
    message prefix (e.g. ``"Tavily research <id>"``). The caller keeps any
    non-``completed`` terminal handling.
    """
    deadline = time.monotonic() + max_wait_seconds
    while status not in RESEARCH_TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise WebToolError(
                WebError(
                    type=WebErrorType.TIMEOUT,
                    message=f"{provider} still '{status}' after {max_wait_seconds:.0f}s",
                )
            )
        await asyncio.sleep(poll_interval)
        status, data = await refresh()
    return status, data


def report_or_empty(content: Any, provider: str) -> str:
    """Unwrap research ``content`` to a markdown report, raising EMPTY if blank.

    Strings pass through; anything else is JSON-encoded. Citation extraction
    stays per-provider; providers that need custom content unwrapping do it
    before calling here.
    """
    report = content if isinstance(content, str) else json.dumps(content or {})
    if not report or report == "{}":
        raise WebToolError(
            WebError(type=WebErrorType.EMPTY, message=f"{provider} research returned no output")
        )
    return report


# Time-range aliases → canonical single-letter form; canonical → day count.
_CANONICAL_RANGE = {
    "h": "h", "hour": "h",
    "d": "d", "day": "d",
    "w": "w", "week": "w",
    "m": "m", "month": "m",
    "y": "y", "year": "y",
}

TIME_RANGE_DAYS = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}


def normalize_time_range(
    time_range: Optional[str], default: Optional[str] = None, *, provider: str = ""
) -> Optional[str]:
    """Canonicalize a time-range token to a single letter (h/d/w/m/y).

    Empty input returns ``default`` unchanged; an unknown non-empty value
    warns and falls back to ``default``.
    """
    if not time_range:
        return default
    canonical = _CANONICAL_RANGE.get(time_range.lower())
    if canonical is None:
        suffix = f" for {provider}" if provider else ""
        logger.warning(f"Invalid time_range '{time_range}'{suffix}, ignoring")
        return default
    return canonical


def time_range_to_days(time_range: Optional[str]) -> Optional[float]:
    """Map a time-range token (aliases accepted) to fractional days, None if unmappable."""
    canonical = _CANONICAL_RANGE.get((time_range or "").lower())
    return TIME_RANGE_DAYS.get(canonical) if canonical else None


def time_range_to_start_date(
    time_range: Optional[str], *, fmt: str, provider: str = ""
) -> Optional[str]:
    """Map a time-range token to a past start-date string via ``fmt``, None if unmappable.

    Shared arithmetic for the search providers; only the ``strftime`` format
    differs (Exa wants an ISO datetime, Parallel a bare date).
    """
    days = time_range_to_days(time_range)
    if days is None:
        if time_range:
            suffix = f" for {provider}" if provider else ""
            logger.warning(f"Invalid time_range '{time_range}'{suffix}, ignoring")
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(fmt)


def is_empty_markdown(markdown: str) -> bool:
    """Whether scraped markdown is too short to count as real page content."""
    return len(markdown.strip()) < 10


def empty_content_error() -> WebError:
    """The EMPTY error for a page that returned no usable content."""
    return WebError(type=WebErrorType.EMPTY, message="Page returned empty content")


def result_card(
    *, title: str, url: str, favicon: Optional[str] = None, snippet: str = ""
) -> Dict[str, str]:
    """The frontend result-card dict for providers whose artifact card is
    exactly ``{title, url, favicon, snippet}``. Providers with extra keys
    (news source/date, publish_time/id) build their own."""
    return {"title": title, "url": url, "favicon": favicon or "", "snippet": snippet}


def error_response(urls: List[str], err: WebError, provider: str) -> FetchResponse:
    """One call-level error fanned out over every URL in the batch."""
    return FetchResponse(
        results=[FetchResult(url=u, error=err) for u in urls], provider=provider
    )


def assemble_results(
    urls: List[str], by_url: Dict[str, FetchResult], provider: str
) -> FetchResponse:
    """Order per-URL results by the request; URLs the provider never
    mentioned come back as EMPTY errors."""
    results = [
        by_url.get(
            u,
            FetchResult(
                url=u,
                error=WebError(type=WebErrorType.EMPTY, message="No content returned for URL"),
            ),
        )
        for u in urls
    ]
    return FetchResponse(results=results, provider=provider)


def lazy(factory: Callable[[], T]) -> Callable[[], T]:
    """Memoize a zero-arg factory. Search builders defer API-wrapper
    construction so a missing key surfaces per call, not at build time."""
    box: List[T] = []

    def get() -> T:
        if not box:
            box.append(factory())
        return box[0]

    return get
