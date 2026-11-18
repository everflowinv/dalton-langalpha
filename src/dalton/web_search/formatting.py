"""Shared search result formatting, date-window resolution, and exceptions for Dalton web/X search."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

from src.tools.utils.validation_utils import validate_date_format
from src.tools.web.providers._shared import (
    SNIPPET_MAX,
    normalize_time_range,
    result_card,
    time_range_to_start_date,
)

EMPTY_RESULTS_MESSAGE = "网页搜索返回空结果"
EMPTY_GENERATION_MESSAGE = "搜索模型这次没有给出回答，可以换个说法重试一次"
ANSWER_ONLY_HEADING = "综合答案（无来源链接）"
ANSWER_WITH_SOURCES_HEADING = "综合答案（来源见下）"

X_RATE_LIMITED_MESSAGE = "X 搜索触发限流，请稍后重试"
X_EMPTY_RESULTS_MESSAGE = "X 搜索返回空结果"
X_ANSWER_ONLY_HEADING = "X 搜索综合摘要（无来源链接）"
X_ANSWER_WITH_SOURCES_HEADING = "X 搜索综合摘要（来源见下）"
X_UNTRUSTED_NOTICE = "以下为 X 平台外部内容，未经核实"
# 无任何来源链接时：正文是模型生成的摘要，不是原帖，不得当事实或引语用。
X_NO_EVIDENCE_TAG = "no_evidence"
X_NO_EVIDENCE_NOTICE = "以下为未经来源支持的摘要，不得作为事实或引语使用"
X_SUMMARY_TRUNCATED_NOTE = "摘要输出异常，已截断"
MAX_X_HANDLES = 20

_X_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

_UNTRUSTED_WRAPPER_RE = re.compile(
    r"^\s*<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>\s*(?:Source:[^\n]*\n---\n)?(.*?)\s*<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>\s*$",
    re.DOTALL,
)

# 生成摘要里的模型控制标记（<|eos|>、<｜eos｜>、<|im_end|>、…）。
_CONTROL_TOKEN_RE = re.compile(r"<[|｜][A-Za-z0-9_]+[|｜]>")
# 引号对（直双引号、弯双引号、中文直角双引号）。无来源摘要按"逐对识别"处理：
# 每个开引号只与自己最近的闭引号配对，不跨到下一对，避免两个短术语被串成一段
# 长引语；配对成功且内部 >= 20 字符的才剥掉两侧引号（保留文字），短引号原样保留。
_QUOTE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ('"', '"'),
    ("“", "”"),
    ("「", "」"),
    ("『", "』"),
)
_LONG_QUOTE_MIN_CHARS = 20

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

# Ordered markers are 1-2 digits, so a sentence opening with a year ("2025. Demand fell.") is not an item.
_LIST_ITEM_START_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s")
_ANY_CITATION_RE = re.compile(
    r"\[\[\d+\]\]\([^)]*\)|(?<!\d)\[\d+\](?!\d)|" + _X_STATUS_URL_IN_TEXT_RE.pattern,
    re.IGNORECASE,
)
_CITATION_TOKEN_RE = re.compile(
    r"\[\[\d+\]\]\([^)]*\)|(?<!\d)\[\d+\](?!\d)|"
    + _X_STATUS_URL_IN_TEXT_RE.pattern
    + r"(?:[/?#][A-Za-z0-9._~%=&/+-]*)*",
    re.IGNORECASE,
)
_MD_CITATION_LINK_RE = re.compile(r"\[\[\d+\]\]\([^)]*\)")


class OpenClawSearchRequestError(RuntimeError):
    """Raised when a search provider returns a structured request/rate-limit error."""

    def __init__(self, message: str, *, code: str = "REQUEST_ERROR") -> None:
        super().__init__(message)
        self.code = code


class OpenClawSearchEmptyResultError(RuntimeError):
    """Raised when search produces neither results/citations nor a synthesized answer."""

    def __init__(self, message: str = EMPTY_RESULTS_MESSAGE) -> None:
        super().__init__(message or EMPTY_RESULTS_MESSAGE)


class OpenClawSearchEmptyGenerationError(RuntimeError):
    """Raised when the search model returned an empty generation (rephrase/retry signal)."""

    def __init__(self, detail: str = "") -> None:
        cleaned = detail.strip()
        super().__init__(EMPTY_GENERATION_MESSAGE if not cleaned else cleaned)


def _answer_with_sources(
    answer: str,
    results: List[Dict[str, Any]],
    *,
    only_heading: str = ANSWER_ONLY_HEADING,
    with_sources_heading: str = ANSWER_WITH_SOURCES_HEADING,
) -> Union[str, List[Dict[str, Any]]]:
    """Place the synthesized answer as a leading ``answer_box`` item when results exist."""
    if not results:
        return f"{only_heading}\n{answer}"
    return [{"type": "answer_box", "title": with_sources_heading, "content": answer}, *results]


def unwrap_untrusted_text(value: Any) -> str:
    """Strip external-untrusted-content wrapper markers when present."""
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
    provider: str = "gemini_grounding",
) -> tuple[Optional[str], Optional[str]]:
    """Resolve tool time filters into ``(date_after, date_before)`` in ``YYYY-MM-DD`` format."""
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
        time_range, default_time_range, provider=provider
    )
    if not effective_range:
        return None, None

    computed_after = time_range_to_start_date(
        effective_range, fmt="%Y-%m-%d", provider=provider
    )
    if not computed_after:
        return None, None
    return computed_after, max(computed_after, today_str)


def extract_broker_payload(broker_response: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap the normalized inner search payload from a provider reply envelope."""
    if not isinstance(broker_response, Mapping):
        raise ValueError("Invalid search response envelope")

    if broker_response.get("ok") is False:
        err = broker_response.get("error")
        if isinstance(err, Mapping):
            code = str(err.get("code") or "REQUEST_ERROR").strip()
            msg = str(err.get("message") or code or "search request failed").strip()
        else:
            code = "REQUEST_ERROR"
            msg = str(err or "search request failed").strip()
        raise OpenClawSearchRequestError(msg, code=code)

    result = broker_response.get("result")
    if isinstance(result, Mapping):
        content_blocks = result.get("content")
        if isinstance(content_blocks, list) and content_blocks:
            first_block = content_blocks[0]
            if isinstance(first_block, Mapping) and isinstance(first_block.get("text"), str):
                parsed = json.loads(first_block["text"])
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
    """Map a normalized web search payload into ``(cleaned_results, artifact)``."""
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
        "search_engine": "gemini_grounding",
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


def _optional_x_bool(name: str, value: Any) -> bool | None:
    """Return a media-understanding flag, or None when the caller omitted it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def _sanitize_generated_summary(
    answer: str, *, has_sources: bool = False
) -> Tuple[str, List[str]]:
    """Strip control tokens and repeated paragraphs from a synthesized X summary."""
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
    """Unquote paired spans of length >= 20 in a no-evidence summary while keeping inner text."""
    out: List[str] = []
    i = 0
    n = len(answer)
    while i < n:
        ch = answer[i]
        pair = next(((o, c) for o, c in _QUOTE_PAIRS if ch == o), None)
        if pair is None:
            out.append(ch)
            i += 1
            continue
        _open, close_ch = pair
        close = answer.find(close_ch, i + 1)
        if close == -1:
            out.append(ch)
            i += 1
            continue
        inner = answer[i + 1 : close]
        if len(inner) >= _LONG_QUOTE_MIN_CHARS and "\n" not in inner:
            out.append(inner)
        else:
            out.append(answer[i : close + 1])
        i = close + 1
    return "".join(out)


def _x_answer_with_sources(
    answer: str,
    results: List[Dict[str, Any]],
    *,
    only_heading: str = X_ANSWER_ONLY_HEADING,
    with_sources_heading: str = X_ANSWER_WITH_SOURCES_HEADING,
) -> Union[str, List[Dict[str, Any]]]:
    """Place the xAI synthesized summary as the leading ``answer_box`` item."""
    if not results:
        return (
            f"{only_heading} [{X_NO_EVIDENCE_TAG}]\n"
            f"{X_NO_EVIDENCE_NOTICE}\n{answer}"
        )
    prefixed_answer = f"{X_UNTRUSTED_NOTICE}\n{answer}" if answer else X_UNTRUSTED_NOTICE
    return [
        {
            "type": "answer_box",
            "title": with_sources_heading,
            "content": prefixed_answer,
            "evidence": "posts_below",
        },
        *results,
    ]


def _parse_x_status_url(url: str) -> Optional[Tuple[str, str]]:
    """Return ``(@handle, status_id)`` only if ``url`` is an X/Twitter status URL."""
    cleaned = url.strip()
    m = _X_STATUS_URL_RE.match(cleaned)
    if not m:
        return None
    return f"@{m.group(1)}", m.group(2)


def decode_snowflake_date(status_id: str) -> Optional[str]:
    """Decode an X/Twitter snowflake status ID into an ISO 8601 UTC timestamp."""
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


def _is_citation_only_line(line: str) -> bool:
    """True when the line is nothing but citation markers and separators."""
    if not _CITATION_TOKEN_RE.search(line):
        return False
    rest = _CITATION_TOKEN_RE.sub("", line)
    return not rest.strip(" \t,;，；、.。!！?？:：()（）[]【】…“”‘’「」『』《》—–\"'")


def _summary_blocks(text: str) -> List[Tuple[str, bool]]:
    """Split a summary into ``(block, is_list_item)`` items or paragraphs."""
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


def _extract_unique_summary_excerpt(
    answer_text: str,
    url: str,
    status_id: str,
    citation_index: int,
) -> Optional[str]:
    """Return the summary description when a list item uniquely cites this X post."""
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
        if _X_USERNAME_RE.fullmatch(h) is None:
            raise ValueError("handles entries must be X usernames")
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
    """Map a normalized ``x_search`` payload into ``(cleaned_results, artifact)``."""
    payload = extract_broker_payload(broker_response)

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
        "search_engine": "xai",
        "provider": provider_name,
        "response_time": round(response_time, 2),
        "total_results": len(cleaned_results),
        "evidence_count": len(cleaned_results),
        "results": cards,
    }
    if not cleaned_results and answer_text:
        artifact["no_evidence"] = True
        answer_text = _strip_quoted_spans(answer_text)
    if summary_issues:
        artifact["summary_issues"] = summary_issues
    if answer_text:
        artifact["answer"] = answer_text
    if isinstance(request_id, str) and request_id:
        artifact["request_id"] = request_id

    return cleaned_results, artifact


__all__ = [
    "ANSWER_ONLY_HEADING",
    "ANSWER_WITH_SOURCES_HEADING",
    "EMPTY_GENERATION_MESSAGE",
    "EMPTY_RESULTS_MESSAGE",
    "MAX_X_HANDLES",
    "OpenClawSearchEmptyGenerationError",
    "OpenClawSearchEmptyResultError",
    "OpenClawSearchRequestError",
    "X_ANSWER_ONLY_HEADING",
    "X_ANSWER_WITH_SOURCES_HEADING",
    "X_EMPTY_RESULTS_MESSAGE",
    "X_NO_EVIDENCE_NOTICE",
    "X_NO_EVIDENCE_TAG",
    "X_RATE_LIMITED_MESSAGE",
    "X_SUMMARY_TRUNCATED_NOTE",
    "X_UNTRUSTED_NOTICE",
    "_answer_with_sources",
    "_extract_unique_summary_excerpt",
    "_favicon_url",
    "_normalize_handles_param",
    "_optional_x_bool",
    "_resolve_date_window",
    "_sanitize_generated_summary",
    "_strip_quoted_spans",
    "_x_answer_with_sources",
    "decode_snowflake_date",
    "extract_broker_payload",
    "map_broker_response",
    "map_x_search_response",
    "unwrap_untrusted_text",
]
