"""OpenClaw X (Twitter) search tool and client for Dalton (`search_api: openclaw`).

Routes `x_search` tool calls from the Dalton backend to the host-side HTTP bridge
(`scripts/dalton/web_search_bridge.py` `POST /x-search` on `127.0.0.1:8961`),
which queues on the shared bridge semaphore and forwards to the OpenClaw gateway
plugin (`POST /dalton-broker/v1/x-search`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import httpx
from langchain_core.tools import tool

from src.dalton.settings import (
    get_dalton_web_search_bridge_queue_timeout_seconds,
    get_dalton_web_search_bridge_token,
    get_dalton_web_search_bridge_url,
)
from src.dalton.web_search.broker_protocol import (
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_MS,
    compute_http_timeout_seconds,
)
from src.dalton.web_search.provider import (
    BRIDGE_UNAVAILABLE_MESSAGE,
    OpenClawSearchBridgeUnavailableError,
    OpenClawSearchEmptyResultError,
    OpenClawSearchRequestError,
    _PARAM_ERROR_CODES,
    _RATE_LIMIT_CODES,
    _favicon_url,
    extract_broker_payload,
    unwrap_untrusted_text,
)
from src.tools.utils.validation_utils import validate_date_format
from src.tools.web.providers._shared import (
    SNIPPET_MAX,
    clip_error,
    error_from_httpx,
    lazy,
    request_json,
    result_card,
)

logger = logging.getLogger(__name__)

X_BRIDGE_UNAVAILABLE_MESSAGE = "OpenClaw X 搜索桥不可用"
X_BRIDGE_BUSY_MESSAGE = "X 搜索繁忙，请稍后重试"
X_RATE_LIMITED_MESSAGE = "X 搜索触发限流，请稍后重试"
X_EMPTY_RESULTS_MESSAGE = "OpenClaw X 搜索返回空结果"
X_ANSWER_ONLY_HEADING = "OpenClaw X 搜索综合摘要（无来源链接）"
X_ANSWER_WITH_SOURCES_HEADING = "OpenClaw X 搜索综合摘要（来源见下）"
X_UNTRUSTED_NOTICE = "以下为 X 平台外部内容，未经核实"
# 无任何来源链接时：正文是模型生成的摘要，不是原帖，不得当事实或引语用。
X_NO_EVIDENCE_TAG = "no_evidence"
X_NO_EVIDENCE_NOTICE = "以下为未经来源支持的摘要，不得作为事实或引语使用"
X_SUMMARY_TRUNCATED_NOTE = "摘要输出异常，已截断"
MAX_X_HANDLES = 20

# 生成摘要里的模型控制标记（<|eos|>、<｜eos｜>、<|im_end|>、…）。
_CONTROL_TOKEN_RE = re.compile(r"<[|｜][A-Za-z0-9_]+[|｜]>")
# 成对引号（直双引号、弯双引号、中文直角双引号）：无来源时仅对长度 >= 20 的引语剥掉两侧引号，保留正文与短术语引号。
_LONG_QUOTED_SPAN_RES = (
    re.compile(r'"([^"\n]{20,})"'),
    re.compile(r"“([^”\n]{20,})”"),
    re.compile(r"「([^」\n]{20,})」"),
    re.compile(r"『([^』\n]{20,})』"),
)

_X_SNOWFLAKE_EPOCH_MS = 1288834974657
_X_MIN_VALID_DT = datetime(2010, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

_X_STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/status/(\d+)(?!\d)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_X_STATUS_URL_IN_TEXT_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,30})/status/(\d+)(?!\d)",
    re.IGNORECASE,
)
_ANY_FOOTNOTE_IN_LINE_RE = re.compile(r"(?<!\d)\[(\d+)\](?!\d)")
_ANY_STATUS_ID_IN_LINE_RE = re.compile(r"/status/(\d+)(?!\d)", re.IGNORECASE)


def _format_x_bridge_unavailable_message(raw_message: str) -> str:
    cleaned = raw_message.strip()
    if cleaned.startswith(X_BRIDGE_UNAVAILABLE_MESSAGE):
        return cleaned
    if cleaned.startswith(BRIDGE_UNAVAILABLE_MESSAGE):
        suffix = cleaned[len(BRIDGE_UNAVAILABLE_MESSAGE) :].lstrip(": ").strip()
        return (
            f"{X_BRIDGE_UNAVAILABLE_MESSAGE}: {suffix}"
            if suffix
            else X_BRIDGE_UNAVAILABLE_MESSAGE
        )
    return (
        f"{X_BRIDGE_UNAVAILABLE_MESSAGE}: {cleaned}"
        if cleaned
        else X_BRIDGE_UNAVAILABLE_MESSAGE
    )


def _raise_for_x_bridge_error_payload(
    payload: Mapping[str, Any],
    *,
    http_status: Optional[int] = None,
) -> None:
    """Classify a structured bridge/broker error payload for `x_search` with X-specific messages."""
    err = payload.get("error")
    if isinstance(err, Mapping):
        code = str(err.get("code") or "").strip()
        raw_message = clip_error(str(err.get("message") or "").strip())
    else:
        code = ""
        raw_message = clip_error(str(err or "").strip())

    if code == "bridge_busy" or code.lower() == "bridge_busy":
        raise OpenClawSearchRequestError(X_BRIDGE_BUSY_MESSAGE, code="bridge_busy")

    if code in _RATE_LIMIT_CODES or http_status == 429:
        detail = (
            f"{X_RATE_LIMITED_MESSAGE}: {raw_message}"
            if raw_message
            else X_RATE_LIMITED_MESSAGE
        )
        raise OpenClawSearchRequestError(
            detail,
            code=code or "PROVIDER_RATE_LIMITED",
        )

    if http_status == 400 or (code in _PARAM_ERROR_CODES and http_status in (None, 400)):
        detail_parts = [p for p in (code, raw_message) if p]
        detail = ": ".join(detail_parts) if detail_parts else "invalid request"
        raise OpenClawSearchRequestError(
            f"Search failed: 参数错误 ({detail})",
            code=code or "INVALID_REQUEST",
        )

    if code and raw_message:
        raise OpenClawSearchBridgeUnavailableError(
            _format_x_bridge_unavailable_message(f"{code}: {raw_message}")
        )
    raise OpenClawSearchBridgeUnavailableError(
        _format_x_bridge_unavailable_message(raw_message or code or "request failed")
    )


def _sanitize_generated_summary(
    answer: str, *, has_sources: bool = False
) -> Tuple[str, List[str]]:
    """Strip generation artifacts from a synthesized summary.

    When ``has_sources`` is True, control tokens are removed in-place without
    discarding trailing text, and repeated paragraphs drop only the duplicate
    paragraph while keeping the first occurrence and subsequent unique
    paragraphs. When ``has_sources`` is False, control tokens and repeated
    paragraphs truncate the summary from the artifact onward.
    """
    issues: List[str] = []
    text = answer

    control = _CONTROL_TOKEN_RE.search(text)
    if control is not None:
        if has_sources:
            text = _CONTROL_TOKEN_RE.sub("", text).strip()
        else:
            text = text[: control.start()].rstrip()
        issues.append("control_marker")

    paragraphs = re.split(r"\n\s*\n", text)
    seen: set[str] = set()
    kept: List[str] = []
    for para in paragraphs:
        key = " ".join(para.split())
        if not key:
            continue
        if key.casefold() in seen:
            if "repeated_block" not in issues:
                issues.append("repeated_block")
            if has_sources:
                continue
            break
        seen.add(key.casefold())
        kept.append(para)
    if "repeated_block" in issues:
        text = "\n\n".join(kept).rstrip()

    return text, issues


def _strip_quoted_spans(answer: str) -> str:
    """Unquote paired quoted spans of length >= 20 in a no-evidence summary
    while keeping their inner text and leaving short quoted terms intact."""
    for pattern in _LONG_QUOTED_SPAN_RES:
        answer = pattern.sub(r"\1", answer)
    return answer


def _x_answer_with_sources(
    answer: str, results: List[Dict[str, Any]]
) -> Union[str, List[Dict[str, Any]]]:
    """Place the xAI synthesized summary (with untrusted external content notice) as the leading `answer_box` item.

    With no post evidence at all the summary is marked `no_evidence` and its
    quoted spans are stripped: nothing in it may be cited as fact or quote."""
    if not results:
        dequoted = _strip_quoted_spans(answer)
        return (
            f"{X_ANSWER_ONLY_HEADING} [{X_NO_EVIDENCE_TAG}]\n"
            f"{X_NO_EVIDENCE_NOTICE}\n{dequoted}"
        )
    prefixed_answer = f"{X_UNTRUSTED_NOTICE}\n{answer}" if answer else X_UNTRUSTED_NOTICE
    return [
        {
            "type": "answer_box",
            "title": X_ANSWER_WITH_SOURCES_HEADING,
            "content": prefixed_answer,
            "evidence": "posts_below",
        },
        *results,
    ]


def _parse_x_status_url(url: str) -> Optional[Tuple[str, str]]:
    """Return `(@handle, status_id)` only if `url` is an X/Twitter status URL, else `None`."""
    cleaned = url.strip()
    m = _X_STATUS_URL_RE.match(cleaned)
    if not m:
        return None
    return f"@{m.group(1)}", m.group(2)


def decode_snowflake_date(status_id: str) -> Optional[str]:
    """Decode an X/Twitter snowflake status ID into an ISO 8601 UTC timestamp.

    Formula: `((id >> 22) + 1288834974657)` ms (UTC).
    Returns `None` if `status_id` is not a valid integer or if the decoded time
    falls outside `[2010-11-01T00:00:00Z, now + 1 day]`.
    """
    if not isinstance(status_id, str) or not status_id.isdigit():
        return None
    try:
        snowflake = int(status_id)
    except ValueError:
        return None
    if snowflake < (1 << 22):
        return None
    timestamp_ms = (snowflake >> 22) + _X_SNOWFLAKE_EPOCH_MS
    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    max_valid_dt = datetime.now(timezone.utc) + timedelta(days=1)
    if dt < _X_MIN_VALID_DT or dt > max_valid_dt:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Ordered markers are 1-2 digits, so a sentence opening with a year ("2025. Demand fell.") is not an item.
_LIST_ITEM_START_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s")


_ANY_CITATION_RE = re.compile(
    r"\[\[\d+\]\]\([^)]*\)|(?<!\d)\[\d+\](?!\d)|" + _X_STATUS_URL_IN_TEXT_RE.pattern, re.IGNORECASE
)
_CITATION_TOKEN_RE = re.compile(
    r"\[\[\d+\]\]\([^)]*\)|(?<!\d)\[\d+\](?!\d)|" + _X_STATUS_URL_IN_TEXT_RE.pattern + r"(?:[/?#][A-Za-z0-9._~%=&/+-]*)*",
    re.IGNORECASE,
)


def _is_citation_only_line(line: str) -> bool:
    """True when the line is nothing but citation markers (and separators/closing punctuation)."""
    if not _CITATION_TOKEN_RE.search(line):
        return False
    rest = _CITATION_TOKEN_RE.sub("", line)
    return not rest.strip(" \t,;，；、.。!！?？:：()（）[]【】…“”‘’「」『』《》—–\"'")


def _summary_blocks(text: str) -> List[Tuple[str, bool]]:
    """Split a summary into `(block, is_list_item)`: one per list item (with its wrapped lines) or paragraph.

    An item runs on through indented lines, even across a blank line, so the one-post check
    sees every citation the item carries. Once the item holds a citation, an unindented line
    is new prose; a line of nothing but citations still belongs to the item.
    """
    blocks: List[Tuple[str, bool]] = []
    buf: List[str] = []
    buf_is_item = False
    pending_blank = False

    def flush() -> None:
        joined = " ".join(" ".join(buf).split())
        if joined:
            blocks.append((joined, buf_is_item))

    for line in text.splitlines():
        if not line.strip():
            pending_blank = True
            continue
        starts_item = bool(_LIST_ITEM_START_RE.match(line))
        indented = line[:1].isspace()
        in_item = buf_is_item and bool(buf)
        if in_item and not starts_item and _is_citation_only_line(line):
            buf.append(line.strip())
            pending_blank = False
            continue
        if in_item and not starts_item and indented:
            buf.append(line.strip())
            pending_blank = False
            continue
        ends_item = in_item and not indented and (
            pending_blank or bool(_ANY_CITATION_RE.search(" ".join(buf)))
        )
        if starts_item or ends_item or (pending_blank and not in_item):
            flush()
            buf = []
            buf_is_item = starts_item
        pending_blank = False
        buf.append(line.strip())
    flush()
    return blocks


_MD_CITATION_LINK_RE = re.compile(r"\[\[\d+\]\]\([^)]*\)")


def _extract_unique_summary_excerpt(
    answer_text: str,
    url: str,
    status_id: str,
    citation_index: int,
) -> Optional[str]:
    """The summary text describing this post, ONLY when a list item cites this post and nothing else.

    xAI lists one post per item (`- **@handle** …: "quote" [[n]](url)`), so such an item is
    the post's description, whole. Running prose gets no excerpt: sentence boundaries in a
    synthesized summary (abbreviations, years, quotes, citation links after the stop) can't
    be found reliably, and a wrong split would hand an uncited claim to a post. The full
    summary already leads the result as the `answer_box`.
    """
    if not answer_text:
        return None

    status_pattern = (
        re.compile(rf"/status/{re.escape(status_id)}(?!\d)", re.IGNORECASE)
        if status_id
        else None
    )
    url_pattern = (
        re.compile(rf"{re.escape(url)}(?![\w/])", re.IGNORECASE) if url else None
    )
    footnote_pattern = (
        re.compile(rf"(?<!\d)\[{citation_index}\](?!\d)")
        if citation_index > 0
        else None
    )

    def cites_this(unit: str) -> bool:
        return bool(
            (url_pattern and url_pattern.search(unit))
            or (status_pattern and status_pattern.search(unit))
            or (footnote_pattern and footnote_pattern.search(unit))
        )

    def cites_only_this(unit: str) -> bool:
        ids = {m.group(1) for m in _ANY_STATUS_ID_IN_LINE_RE.finditer(unit)}
        if ids and not ids <= {status_id}:
            return False
        notes = {int(m.group(1)) for m in _ANY_FOOTNOTE_IN_LINE_RE.finditer(unit)}
        return not notes or (citation_index > 0 and notes <= {citation_index})

    candidates: List[str] = []
    for block, is_list_item in _summary_blocks(answer_text):
        if is_list_item and cites_this(block) and cites_only_this(block):
            candidates.append(block)

    if len(candidates) != 1:
        return None

    snippet = _MD_CITATION_LINK_RE.sub("", candidates[0])
    if url_pattern:
        snippet = url_pattern.sub("", snippet)
    if footnote_pattern:
        snippet = footnote_pattern.sub("", snippet)
    snippet = re.sub(r"\[\]\(\)", "", snippet)
    snippet = snippet.replace("**", "")
    snippet = re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+", "", snippet).strip()
    snippet = " ".join(snippet.split())
    return snippet or None


def _normalize_handles_param(handles: Optional[List[str]]) -> Optional[List[str]]:
    if handles is None:
        return None
    if not isinstance(handles, list):
        raise ValueError("handles must be a list of strings")
    cleaned: List[str] = []
    for item in handles:
        if not isinstance(item, str):
            raise ValueError("handles entries must be strings")
        h = item.strip().lstrip("@").strip()
        if not h:
            raise ValueError("handles entries must not be empty")
        cleaned.append(h)
    if len(cleaned) > MAX_X_HANDLES:
        raise ValueError(f"handles cannot exceed {MAX_X_HANDLES} accounts")
    return cleaned or None


def map_x_search_response(
    broker_response: Mapping[str, Any],
    *,
    query: str,
    response_time: float = 0.0,
    include_favicon: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Map an OpenClaw `x_search` broker reply into `(cleaned_results, artifact)`.

    Only URLs of the form `https://(x|twitter).com/<handle>/status/<id>` become
    `x_post` entries:
    - `author`: `@<handle>` from the URL
    - `date`: UTC timestamp decoded strictly from the status snowflake ID (`None` if out of range)
    - `summary_excerpt`: the summary sentence uniquely citing this post (`None` if absent or shared across citations)
    - `url`: canonical X status URL
    """
    if isinstance(broker_response, Mapping) and broker_response.get("ok") is False:
        _raise_for_x_bridge_error_payload(broker_response)

    try:
        payload = extract_broker_payload(broker_response)
    except OpenClawSearchBridgeUnavailableError as exc:
        raise OpenClawSearchBridgeUnavailableError(
            _format_x_bridge_unavailable_message(str(exc))
        ) from exc

    if isinstance(payload.get("details"), Mapping):
        payload = dict(payload["details"])

    provider_name = str(
        payload.get("provider") or broker_response.get("provider") or "xai"
    )
    request_id = broker_response.get("providerRequestId") or payload.get("requestId")
    raw_answer_text = unwrap_untrusted_text(
        payload.get("content") or payload.get("answer") or ""
    )

    cleaned_results: List[Dict[str, Any]] = []
    cards: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    raw_sources: List[Tuple[str, str, str, str, int]] = []

    raw_citations = payload.get("inlineCitations") or payload.get("citations")
    if isinstance(raw_citations, list) and raw_citations:
        for idx, citation in enumerate(raw_citations, start=1):
            if isinstance(citation, str):
                url = unwrap_untrusted_text(citation)
                cit_title = ""
            elif isinstance(citation, Mapping):
                url = unwrap_untrusted_text(
                    str(citation.get("url") or citation.get("link") or "")
                )
                cit_title = unwrap_untrusted_text(
                    str(citation.get("title") or "")
                )
            else:
                continue

            parsed_status = _parse_x_status_url(url)
            if parsed_status is None or url in seen_urls:
                continue
            seen_urls.add(url)
            author, status_id = parsed_status
            raw_sources.append((url, author, status_id, cit_title, idx))

    if not raw_sources and raw_answer_text:
        for idx, match in enumerate(
            _X_STATUS_URL_IN_TEXT_RE.finditer(raw_answer_text), start=1
        ):
            url = match.group(0)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            author = f"@{match.group(1)}"
            status_id = match.group(2)
            raw_sources.append((url, author, status_id, "", idx))

    has_sources = bool(raw_sources)
    answer_text, summary_issues = _sanitize_generated_summary(
        raw_answer_text, has_sources=has_sources
    )

    for url, author, status_id, cit_title, idx in raw_sources:
        post_date = decode_snowflake_date(status_id)
        summary_excerpt = _extract_unique_summary_excerpt(
            answer_text, url, status_id, idx
        )
        if summary_excerpt is None and answer_text != raw_answer_text:
            summary_excerpt = _extract_unique_summary_excerpt(
                raw_answer_text, url, status_id, idx
            )
        title = cit_title or (
            f"{author} ({post_date})" if post_date else author
        )
        row: Dict[str, Any] = {
            "type": "x_post",
            "author": author,
            "date": post_date,
            "summary_excerpt": summary_excerpt,
            "url": url,
            "title": title,
        }
        cleaned_results.append(row)
        favicon = _favicon_url(url) if include_favicon else ""
        cards.append(
            result_card(
                title=title,
                url=url,
                favicon=favicon,
                snippet=(summary_excerpt or "")[:SNIPPET_MAX],
            )
        )

    if summary_issues:
        answer_text = f"{answer_text}\n[{X_SUMMARY_TRUNCATED_NOTE}]"

    if not cleaned_results and not answer_text:
        raise OpenClawSearchEmptyResultError(X_EMPTY_RESULTS_MESSAGE)

    artifact: Dict[str, Any] = {
        "type": "web_search",
        "query": query,
        "search_engine": "openclaw_x",
        "provider": provider_name,
        "response_time": round(response_time, 2),
        "total_results": len(cleaned_results),
        # Evidence vs generated summary, as separate fields: the posts are the
        # citable evidence, the answer is a synthesis of them (or, with
        # evidence_count 0, of nothing).
        "evidence_count": len(cleaned_results),
        "results": cards,
    }
    if not cleaned_results and answer_text:
        # Nothing backs the summary: flag it and strip quote-shaped spans so
        # no layer returns a fabricated "direct quote".
        artifact["no_evidence"] = True
        answer_text = _strip_quoted_spans(answer_text)
    if summary_issues:
        artifact["summary_issues"] = summary_issues
    if answer_text:
        artifact["answer"] = answer_text
    if isinstance(request_id, str) and request_id:
        artifact["request_id"] = request_id

    return cleaned_results, artifact


class OpenClawXSearchWrapper:
    """HTTP client for the host-side OpenClaw X search bridge endpoint (`POST /x-search`)."""

    def __init__(
        self,
        *,
        bridge_url: Optional[str] = None,
        bridge_token: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        resolved_url = (
            bridge_url or get_dalton_web_search_bridge_url()
        ).strip().rstrip("/")
        if resolved_url.endswith("/search"):
            resolved_url = resolved_url[: -len("/search")]
        resolved_token = (
            bridge_token
            if bridge_token is not None
            else get_dalton_web_search_bridge_token()
        ).strip()
        if not resolved_token:
            raise OpenClawSearchBridgeUnavailableError(
                f"{X_BRIDGE_UNAVAILABLE_MESSAGE}: DALTON_WEB_SEARCH_BRIDGE_TOKEN 未配置"
            )
        self._bridge_url = resolved_url
        self._bridge_token = resolved_token
        self._timeout_seconds: Optional[float] = (
            float(timeout_seconds) if timeout_seconds is not None else None
        )

    async def raw_results(
        self,
        query: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        handles: Optional[List[str]] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> Dict[str, Any]:
        """Send one X search request to the host-side bridge and return the broker response."""
        payload: Dict[str, Any] = {
            "query": query,
            "timeoutMs": int(timeout_ms),
        }
        if handles:
            payload["allowed_x_handles"] = handles
        if from_date is not None:
            payload["from_date"] = from_date
        if to_date is not None:
            payload["to_date"] = to_date

        endpoint = (
            self._bridge_url
            if self._bridge_url.endswith("/x-search")
            else f"{self._bridge_url}/x-search"
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
                provider="OpenClawXSearchBridge",
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
                _raise_for_x_bridge_error_payload(
                    parsed_body,
                    http_status=exc.response.status_code,
                )
            err = error_from_httpx(exc)
            raise OpenClawSearchBridgeUnavailableError(
                _format_x_bridge_unavailable_message(
                    clip_error(err.message or str(exc))
                )
            ) from exc
        except httpx.HTTPError as exc:
            err = error_from_httpx(exc)
            raise OpenClawSearchBridgeUnavailableError(
                _format_x_bridge_unavailable_message(
                    clip_error(err.message or str(exc))
                )
            ) from exc
        except OSError as exc:
            raise OpenClawSearchBridgeUnavailableError(
                _format_x_bridge_unavailable_message(clip_error(str(exc)))
            ) from exc

        if not isinstance(response, Mapping):
            raise OpenClawSearchBridgeUnavailableError(
                _format_x_bridge_unavailable_message("Invalid JSON response from bridge")
            )
        if response.get("ok") is False:
            _raise_for_x_bridge_error_payload(response)
        return dict(response)


def build_x_search_tool(
    include_favicon: bool = True,
    wrapper: Optional[OpenClawXSearchWrapper] = None,
):
    """Build a direct `x_search` tool backed by OpenClaw's xAI X search."""
    _get_api_wrapper = (
        (lambda: wrapper) if wrapper is not None else lazy(OpenClawXSearchWrapper)
    )

    @tool("x_search", response_format="content_and_artifact")
    async def x_search(
        query: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        handles: Optional[List[str]] = None,
    ) -> Tuple[Union[List[Dict[str, Any]], str], Dict[str, Any]]:
        """Search X (Twitter) posts for social sentiment, management or KOL remarks, first-hand reactions to breaking events, and original posts.

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

            api = _get_api_wrapper()
            start_t = time.monotonic()
            raw = await api.raw_results(
                query=query.strip(),
                from_date=from_date,
                to_date=to_date,
                handles=normalized_handles,
            )
            elapsed = time.monotonic() - start_t
            cleaned_results, artifact = map_x_search_response(
                raw,
                query=query.strip(),
                response_time=elapsed,
                include_favicon=include_favicon,
            )
            if artifact.get("answer"):
                return _x_answer_with_sources(artifact["answer"], cleaned_results), artifact
            if cleaned_results:
                cleaned_results[0] = {"notice": X_UNTRUSTED_NOTICE, **cleaned_results[0]}
            return cleaned_results, artifact
        except OpenClawSearchEmptyResultError as exc:
            error_message = str(exc)
            logger.warning("OpenClaw x_search returned empty results")
            return error_message, {"error": error_message, "query": query}
        except OpenClawSearchRequestError as exc:
            error_message = str(exc)
            if exc.code == "bridge_busy":
                error_message = X_BRIDGE_BUSY_MESSAGE
            elif exc.code in _RATE_LIMIT_CODES and error_message.startswith("OpenClaw 网页搜索"):
                error_message = error_message.replace(
                    "OpenClaw 网页搜索触发限流，请稍后重试",
                    X_RATE_LIMITED_MESSAGE,
                    1,
                )
            logger.warning("OpenClaw x_search request error: %s", exc.code)
            return error_message, {"error": error_message, "code": exc.code, "query": query}
        except OpenClawSearchBridgeUnavailableError as exc:
            error_message = _format_x_bridge_unavailable_message(str(exc))
            logger.error("OpenClaw x_search bridge unavailable")
            return error_message, {"error": error_message, "query": query}
        except json.JSONDecodeError as exc:
            error_message = f"{X_BRIDGE_UNAVAILABLE_MESSAGE}: response is not JSON"
            logger.error("OpenClaw x_search bridge returned non-JSON: %s", type(exc).__name__)
            return error_message, {"error": error_message, "query": query}
        except ValueError as exc:
            error_message = f"Search failed: 参数错误 ({exc})"
            logger.warning("OpenClaw x_search parameter error: %s", exc)
            return error_message, {"error": error_message, "query": query}
        except (httpx.HTTPError, OSError) as exc:
            error_message = f"{X_BRIDGE_UNAVAILABLE_MESSAGE}: {clip_error(str(exc))}"
            logger.error("OpenClaw x_search transport error: %s", type(exc).__name__)
            return error_message, {"error": error_message, "query": query}
        except Exception as exc:
            logger.error("OpenClaw x_search failed: %s", type(exc).__name__)
            error_message = f"Search failed: {exc}"
            return error_message, {"error": str(exc), "query": query}

    return x_search


__all__ = [
    "MAX_X_HANDLES",
    "OpenClawXSearchWrapper",
    "X_ANSWER_ONLY_HEADING",
    "X_ANSWER_WITH_SOURCES_HEADING",
    "X_BRIDGE_BUSY_MESSAGE",
    "X_BRIDGE_UNAVAILABLE_MESSAGE",
    "X_EMPTY_RESULTS_MESSAGE",
    "X_NO_EVIDENCE_NOTICE",
    "X_NO_EVIDENCE_TAG",
    "X_RATE_LIMITED_MESSAGE",
    "X_SUMMARY_TRUNCATED_NOTE",
    "X_UNTRUSTED_NOTICE",
    "build_x_search_tool",
    "decode_snowflake_date",
    "map_x_search_response",
]
