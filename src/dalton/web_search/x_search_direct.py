"""X search through the xAI Responses API, the same route as grok-4.3-oauth.

The base URL is the ``xai-oauth`` provider in ``native_overlay.json``. Request
headers come from ``ChatGrokOpenAI`` (the chat client), not a second copy of
those constants. The body asks for the ``x_search`` tool on ``grok-4.3`` with
``store: false``. ``stream: true`` matches ``ChatGrokOpenAI._get_request_payload``,
which forces it; the CLI proxy rejects a non-streaming body. The HTTP client
still reads the whole response. ``max_turns`` is the assumed name for
OpenClaw's ``maxTurns`` (2) — a live call has to confirm the field.

``inlineCitations`` stays off: citation URLs are a side list, and the answer
text is not rewritten with ``[n]`` markers. Every user shares the first
configured admin's xAI OAuth token. There is no API-key fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import httpx
from langchain_core.tools import tool

from src.dalton.web_search.cache import get_cached_search, search_cache_key, store_cached_search
from src.dalton.web_search.provider import (
    OpenClawSearchEmptyResultError,
    OpenClawSearchRequestError,
)
from src.dalton.web_search.x_search import (
    X_RATE_LIMITED_MESSAGE,
    X_UNTRUSTED_NOTICE,
    _normalize_handles_param,
    _optional_x_bool,
    _x_answer_with_sources,
    map_x_search_response,
)
from src.tools.utils.validation_utils import validate_date_format

logger = logging.getLogger(__name__)

X_SEARCH_MODEL = "grok-4.3"
X_SEARCH_MAX_TURNS = 2
DEFAULT_TIMEOUT_SECONDS = 45.0
SEARCH_ENGINE = "xai"
PROVIDER_NAME = "xai"
X_UNAVAILABLE_MESSAGE = "X 搜索暂不可用"
ANSWER_ONLY_HEADING = "X 搜索综合摘要（无来源链接）"
ANSWER_WITH_SOURCES_HEADING = "X 搜索综合摘要（来源见下）"
# Model-written inline markers. Ordinary Markdown links are left in place.
_INLINE_CITATION_RE = re.compile(r"\[\[\d+\]\]\([^)]+\)")

# Header names the chat client writes in ``model_post_init``. Values are read
# back from that client so the token-auth marker and client version stay in
# one place.
_OAUTH_HEADER_NAMES = (
    "Authorization",
    "X-XAI-Token-Auth",
    "x-grok-client-version",
    "x-grok-model-override",
    "x-client-request-id",
)


class XSearchUnavailable(Exception):
    """The xAI call cannot be completed. The message is safe to show."""


def xai_oauth_base_url() -> str:
    """``providers.provider_config.xai-oauth.base_url`` from the native overlay."""
    from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_overlay_data

    data = load_overlay_data(NATIVE_OVERLAY_PATH)
    providers = data.get("providers") if isinstance(data, Mapping) else None
    config = providers.get("provider_config") if isinstance(providers, Mapping) else None
    entry = config.get("xai-oauth") if isinstance(config, Mapping) else None
    base = entry.get("base_url") if isinstance(entry, Mapping) else None
    url = str(base or "").strip().rstrip("/")
    if not url:
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    return url


def oauth_request(token: str, *, base_url: str) -> tuple[str, dict[str, str]]:
    """Responses URL and OAuth headers from one ``ChatGrokOpenAI`` instance."""
    from src.dalton.llm.providers.xai_grok.client import ChatGrokOpenAI

    client = ChatGrokOpenAI(
        model=X_SEARCH_MODEL,
        api_key=token,
        base_url=base_url,
        session_id=str(uuid.uuid4()),
    )
    source = {
        str(key).lower(): str(value)
        for key, value in dict(client.default_headers or {}).items()
    }
    headers: dict[str, str] = {}
    for name in _OAUTH_HEADER_NAMES:
        value = source.get(name.lower(), "").strip()
        if not value:
            raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
        headers[name] = value
    root = str(client.openai_api_base or "").strip().rstrip("/")
    if not root:
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    return f"{root}/responses", headers


async def pm_xai_access_token() -> str:
    """The first configured admin's ``xai-oauth`` access token.

    Shared by every user, the same way the OpenClaw broker used one account.
    ``None``, a refresh failure, or a plan with no API access is unavailable.
    The token value is never logged.
    """
    from src.dalton.settings import get_configured_pm_user_ids
    from src.server.services.oauth_health import (
        OAuthReauthRequired,
        OAuthRefreshUnavailable,
        valid_token,
    )
    from src.server.services.oauth_protocol import OAuthPlanNoApiAccess

    pm_ids = get_configured_pm_user_ids()
    pm_user_id = str(pm_ids[0]).strip() if pm_ids else ""
    if not pm_user_id:
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    try:
        token_set = await valid_token(pm_user_id, "xai-oauth")
    except (OAuthReauthRequired, OAuthRefreshUnavailable, OAuthPlanNoApiAccess):
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE) from None
    except Exception as exc:
        logger.error(
            "x search provider=%s model=%s status=token elapsed_ms=0 error=%s",
            PROVIDER_NAME,
            X_SEARCH_MODEL,
            type(exc).__name__,
        )
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE) from None
    access = getattr(token_set, "access_token", None) if token_set is not None else None
    if not isinstance(access, str) or not access.strip():
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    return access


def _request_body(
    query: str,
    *,
    from_date: Optional[str],
    to_date: Optional[str],
    handles: Optional[List[str]],
    enable_image_understanding: Optional[bool] = None,
    enable_video_understanding: Optional[bool] = None,
) -> Dict[str, Any]:
    image_flag = _optional_x_bool(
        "enable_image_understanding", enable_image_understanding
    )
    video_flag = _optional_x_bool(
        "enable_video_understanding", enable_video_understanding
    )
    tool_def: Dict[str, Any] = {"type": "x_search"}
    if handles:
        tool_def["allowed_x_handles"] = handles
    if from_date is not None:
        tool_def["from_date"] = from_date
    if to_date is not None:
        tool_def["to_date"] = to_date
    if image_flag is not None:
        tool_def["enable_image_understanding"] = image_flag
    if video_flag is not None:
        tool_def["enable_video_understanding"] = video_flag
    return {
        "model": X_SEARCH_MODEL,
        "input": query,
        "tools": [tool_def],
        "store": False,
        # Forced for the same reason ChatGrokOpenAI._get_request_payload sets it.
        "stream": True,
        # Assumed field for OpenClaw's maxTurns. Not confirmed on a live call.
        "max_turns": X_SEARCH_MAX_TURNS,
    }


def _citations_from_annotations(part: Mapping[str, Any], seen: set[str]) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    annotations = part.get("annotations")
    if not isinstance(annotations, list):
        return found
    for ann in annotations:
        if not isinstance(ann, Mapping) or ann.get("type") != "url_citation":
            continue
        url = str(ann.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "title": str(ann.get("title") or "").strip()})
    return found


def text_and_citations(response: Mapping[str, Any]) -> tuple[str, List[Dict[str, str]]]:
    """Answer text plus ``url_citation`` annotations and top-level citations.

    The text is copied as the model wrote it, including ``[[n]](url)``
    markers. Mapping reads that original text to attach posts and excerpts.
    The tool removes those markers from the display text afterwards. Other
    Markdown links stay. Markers are not inserted.
    """
    texts: List[str] = []
    citations: List[Dict[str, str]] = []
    seen: set[str] = set()
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
                citations.extend(_citations_from_annotations(part, seen))
    top = response.get("citations")
    if isinstance(top, list):
        for item in top:
            if isinstance(item, str):
                url = item.strip()
                title = ""
            elif isinstance(item, Mapping):
                url = str(item.get("url") or item.get("link") or "").strip()
                title = str(item.get("title") or "").strip()
            else:
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            citations.append({"url": url, "title": title})
    return "\n\n".join(texts), citations


def _reject_unfinished(response: Mapping[str, Any]) -> Dict[str, Any]:
    """Refuse a response that failed, stopped early, or carries ``error``.

    A missing ``status`` is still a finished body: the successful live call
    is not required to send one. ``failed`` and ``incomplete`` are
    unavailable even when ``output`` already holds partial text, and that
    text is not returned. Any ``error`` value other than null is the same.
    """
    if not isinstance(response, dict):
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    if response.get("error") not in (None, ""):
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    status = response.get("status")
    if status is not None and status != "completed":
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    return response


def parse_responses_body(raw_text: str) -> Dict[str, Any]:
    """A JSON body, or the ``response`` object of an SSE ``response.completed`` event.

    ``[DONE]`` is ignored. A body that is already the response object (it has
    ``output``) is returned as-is. ``failed``, ``incomplete``, and an
    ``error`` value are unavailable and are not turned into a payload.
    """
    raw = raw_text.lstrip("\ufeff").lstrip()
    if not raw:
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    if raw[0] == "{":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE) from None
        if isinstance(parsed, dict):
            return _reject_unfinished(parsed)
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)

    completed: Optional[Dict[str, Any]] = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        data = stripped[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        response = event.get("response")
        if event.get("type") == "response.completed" and isinstance(response, dict):
            completed = response
        elif completed is None and isinstance(event.get("output"), list):
            completed = event
    if completed is None:
        raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
    return _reject_unfinished(completed)


def to_broker_payload(response: Mapping[str, Any]) -> Dict[str, Any]:
    """The envelope ``map_x_search_response`` already knows how to read.

    Citations are the side list (``inlineCitations`` is not set), so the
    mapper does not look for footnote markers the model was not asked to write.
    """
    response = _reject_unfinished(response)
    text, citations = text_and_citations(response)
    inner = {
        "provider": PROVIDER_NAME,
        "content": text,
        "citations": citations,
    }
    envelope: Dict[str, Any] = {
        "ok": True,
        "provider": PROVIDER_NAME,
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
    }
    response_id = response.get("id")
    if isinstance(response_id, str) and response_id.strip():
        envelope["providerRequestId"] = response_id.strip()
    return envelope


def _raise_for_status(status: int) -> None:
    if status == 429:
        raise OpenClawSearchRequestError(X_RATE_LIMITED_MESSAGE, code="PROVIDER_RATE_LIMITED")
    if status == 400:
        raise OpenClawSearchRequestError(
            "Search failed: 参数错误 (HTTP 400)",
            code="INVALID_REQUEST",
        )
    detail = f"{X_UNAVAILABLE_MESSAGE} (HTTP {status})" if status else X_UNAVAILABLE_MESSAGE
    raise XSearchUnavailable(detail)


class DirectXSearchClient:
    """One Responses API call with the ``x_search`` tool."""

    def __init__(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._timeout = (
            float(timeout_seconds) if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
        )
        self._transport = transport
        # A test token skips the admin lookup. Production leaves this unset.
        self._token = token
        self._base_url = base_url.strip().rstrip("/") if isinstance(base_url, str) else None

    async def raw_results(
        self,
        query: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        handles: Optional[List[str]] = None,
        *,
        enable_image_understanding: Optional[bool] = None,
        enable_video_understanding: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST one x_search turn and return a broker-shaped payload."""
        started = time.monotonic()
        status: int | str = "-"
        try:
            token = self._token if self._token is not None else await pm_xai_access_token()
            if not str(token).strip():
                raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE)
            base_url = self._base_url or xai_oauth_base_url()
            url, headers = oauth_request(token, base_url=base_url)
            body = _request_body(
                query,
                from_date=from_date,
                to_date=to_date,
                handles=handles,
                enable_image_understanding=enable_image_understanding,
                enable_video_understanding=enable_video_understanding,
            )
            # Wall clock for the whole POST and body read. A drip of SSE
            # chunks resets httpx's per-read timer, so it cannot extend this.
            try:
                async with asyncio.timeout(self._timeout):
                    async with httpx.AsyncClient(
                        timeout=self._timeout, transport=self._transport
                    ) as http:
                        response = await http.post(url, headers=headers, json=body)
                        raw_text = response.text
            except TimeoutError:
                status = "timeout"
                raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE) from None
            status = response.status_code
            if status >= 400:
                _raise_for_status(status)
            parsed = parse_responses_body(raw_text)
            return to_broker_payload(parsed)
        except (OpenClawSearchRequestError, XSearchUnavailable):
            raise
        except httpx.HTTPError:
            status = "transport"
            raise XSearchUnavailable(X_UNAVAILABLE_MESSAGE) from None
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "x search provider=%s model=%s status=%s elapsed_ms=%s",
                PROVIDER_NAME,
                X_SEARCH_MODEL,
                status,
                elapsed_ms,
            )


def build_direct_x_search_tool(
    include_favicon: bool = True,
    client: Optional[DirectXSearchClient] = None,
):
    """Build a direct ``x_search`` tool backed by the xAI Responses API."""

    def _client() -> DirectXSearchClient:
        if client is not None:
            return client
        return DirectXSearchClient()

    @tool("x_search", response_format="content_and_artifact")
    async def x_search(
        query: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        handles: Optional[List[str]] = None,
        enable_image_understanding: Optional[bool] = None,
        enable_video_understanding: Optional[bool] = None,
    ) -> Tuple[Union[List[Dict[str, Any]], str], Dict[str, Any]]:
        """Search X (Twitter) posts for social sentiment, management or KOL remarks, first-hand reactions to breaking events, and original posts.
        Set `enable_image_understanding` or `enable_video_understanding` only when this call must read images or video; omit both to leave the xAI request unchanged.

        Args:
            query: Natural-language topic, ticker, or event to search on X. Put date bounds in `from_date`/`to_date` and account filters in `handles` rather than inside `query`.
            from_date: Inclusive start date in YYYY-MM-DD format.
            to_date: Inclusive end date in YYYY-MM-DD format.
            handles: Optional list of X handles (up to 20, with or without leading `@`) to restrict results to specific accounts.

        Returns:
            Synthesized summary (`answer_box`, when available) followed by matching X status posts (`author`, `date` decoded from status ID, `url`, and `summary_excerpt` — the summary's description of that post when uniquely matched, not the original post's full text); only the posts are citable evidence — a summary returned with no source posts is marked `no_evidence` and must not be used as fact or quote, and a degraded summary is truncated and marked 摘要输出异常.
        """
        try:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must not be empty")
            validate_date_format(from_date)
            validate_date_format(to_date)
            if from_date and to_date and from_date > to_date:
                raise ValueError("from_date must not be later than to_date")
            normalized_handles = _normalize_handles_param(handles)
            image_flag = _optional_x_bool(
                "enable_image_understanding", enable_image_understanding
            )
            video_flag = _optional_x_bool(
                "enable_video_understanding", enable_video_understanding
            )
            sent_query = query.strip()

            api = _client()
            cache_key = search_cache_key(
                SEARCH_ENGINE,
                {
                    "model": X_SEARCH_MODEL,
                    "query": sent_query,
                    "from_date": from_date,
                    "to_date": to_date,
                    "handles": normalized_handles,
                    "include_favicon": include_favicon,
                    "enable_image_understanding": image_flag,
                    "enable_video_understanding": video_flag,
                },
            )
            cached = await get_cached_search(cache_key)
            if cached is not None:
                logger.info(
                    "x search provider=%s model=%s status=hit elapsed_ms=0",
                    PROVIDER_NAME,
                    X_SEARCH_MODEL,
                )
                return cached

            started = time.monotonic()
            raw = await api.raw_results(
                query=sent_query,
                from_date=from_date,
                to_date=to_date,
                handles=normalized_handles,
                enable_image_understanding=image_flag,
                enable_video_understanding=video_flag,
            )
            elapsed = time.monotonic() - started
            cleaned, artifact = map_x_search_response(
                raw,
                query=sent_query,
                response_time=elapsed,
                include_favicon=include_favicon,
            )
            artifact["search_engine"] = SEARCH_ENGINE
            artifact["provider"] = PROVIDER_NAME
            # Posts and excerpts already used the original text. Strip
            # ``[[n]](url)`` only from the text shown to the model.
            if isinstance(artifact.get("answer"), str):
                artifact["answer"] = _INLINE_CITATION_RE.sub("", artifact["answer"])
            if artifact.get("answer"):
                content: Union[List[Dict[str, Any]], str] = _x_answer_with_sources(
                    artifact["answer"],
                    cleaned,
                    only_heading=ANSWER_ONLY_HEADING,
                    with_sources_heading=ANSWER_WITH_SOURCES_HEADING,
                )
            else:
                content = cleaned
                if cleaned:
                    cleaned[0] = {"notice": X_UNTRUSTED_NOTICE, **cleaned[0]}
            await store_cached_search(cache_key, content, artifact)
            return content, artifact
        except OpenClawSearchEmptyResultError as exc:
            error_message = str(exc)
            logger.warning("x search provider=%s model=%s status=empty elapsed_ms=0", PROVIDER_NAME, X_SEARCH_MODEL)
            return error_message, {"error": error_message, "query": query}
        except OpenClawSearchRequestError as exc:
            error_message = str(exc)
            logger.warning(
                "x search provider=%s model=%s status=%s elapsed_ms=0",
                PROVIDER_NAME,
                X_SEARCH_MODEL,
                exc.code,
            )
            return error_message, {"error": error_message, "code": exc.code, "query": query}
        except XSearchUnavailable as exc:
            error_message = str(exc) or X_UNAVAILABLE_MESSAGE
            logger.error(
                "x search provider=%s model=%s status=unavailable elapsed_ms=0",
                PROVIDER_NAME,
                X_SEARCH_MODEL,
            )
            return error_message, {"error": error_message, "query": query}
        except ValueError as exc:
            error_message = f"Search failed: 参数错误 ({exc})"
            logger.warning("x search provider=%s model=%s status=invalid elapsed_ms=0", PROVIDER_NAME, X_SEARCH_MODEL)
            return error_message, {"error": error_message, "query": query}
        except Exception as exc:
            logger.error(
                "x search provider=%s model=%s status=error elapsed_ms=0 error=%s",
                PROVIDER_NAME,
                X_SEARCH_MODEL,
                type(exc).__name__,
            )
            return X_UNAVAILABLE_MESSAGE, {"error": X_UNAVAILABLE_MESSAGE, "query": query}

    return x_search
