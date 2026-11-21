from typing import Any, Callable, Dict, Optional, Type, Union, Tuple
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
import asyncio
import contextlib
# DALTON-HOOK(workspace-dedup): ContextVar for redacting raw parse errors in debug logs during dedup calls.
from contextvars import ContextVar
# DALTON-HOOK(complete-parse-failure-billing): timestamp each upstream request when sent.
from datetime import datetime, timezone
import logging
import json
from .token_counter import extract_token_usage
from .content_utils import (
    format_llm_content,
    get_message_content,
)

# Configure logging for token usage
logger = logging.getLogger(__name__)

# DALTON-HOOK(workspace-dedup): opt-in context manager that suppresses raw parse error text in debug logs/exceptions.
_REDACT_PARSE_ERROR_LOGS: ContextVar[bool] = ContextVar(
    "dalton_redact_parse_error_logs",
    default=False,
)


@contextlib.contextmanager
def redact_parse_error_logs():
    """Within this context, log only the exception class name when parsing retries exhaust."""
    token = _REDACT_PARSE_ERROR_LOGS.set(True)
    try:
        yield
    finally:
        _REDACT_PARSE_ERROR_LOGS.reset(token)


# DALTON-HOOK(complete-parse-failure-billing): carry accumulated provider usage on exhausted
# structured-output parse errors and expose an opt-in per-call usage collector so callers can
# bill completed upstream calls even when parsing fails or a later retry times out/aborts.
class StructuredOutputParseError(ValueError):
    """Raised when structured-output parsing exhausts retries; carries accumulated provider usage."""

    def __init__(
        self,
        message: str,
        *,
        token_usage: Optional[Dict[str, Any]] = None,
        retry_attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.token_usage: Dict[str, Any] = dict(token_usage) if isinstance(token_usage, dict) else {}
        self.retry_attempts = int(retry_attempts)
        self.parsing_failed = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# DALTON-HOOK(complete-parse-failure-billing): per-request usage bucket with timing and unknown-call counters.
class _ApiCallUsageBucket(list):
    """List of collected per-call provider usage dicts with per-request timing and unknown-call tracking."""

    def __init__(
        self,
        *,
        is_unbilled_error: Optional[Callable[[BaseException], bool]] = None,
    ) -> None:
        super().__init__()
        self.sent_calls: int = 0
        self.in_flight_calls: int = 0
        self.unknown_calls: int = 0
        self.tracking_active: bool = False
        self.is_unbilled_error: Optional[Callable[[BaseException], bool]] = is_unbilled_error
        self.call_records: list[Dict[str, Any]] = []


_USAGE_ACCUMULATOR: ContextVar[Optional[_ApiCallUsageBucket]] = ContextVar(
    "dalton_api_call_usage_accumulator",
    default=None,
)
_REPORTED_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_5m_tokens",
    "cache_1h_tokens",
)


# DALTON-HOOK(complete-parse-failure-billing): opt-in context manager and pre-send tracking activation.
@contextlib.contextmanager
def collect_api_call_usage(
    *,
    is_unbilled_error: Optional[Callable[[BaseException], bool]] = None,
):
    """Collect per-call provider token usage dicts emitted by ``make_api_call`` within this context."""
    bucket = _ApiCallUsageBucket(is_unbilled_error=is_unbilled_error)
    token = _USAGE_ACCUMULATOR.set(bucket)
    try:
        yield bucket
    finally:
        _USAGE_ACCUMULATOR.reset(token)


def mark_api_call_tracking_active() -> None:
    """Mark the active collector as observing a real LLMService / make_api_call execution
    so zero-send pre-call failures (e.g. resolve_llm_config timeout) are not flagged unknown."""
    bucket = _USAGE_ACCUMULATOR.get()
    if bucket is not None:
        bucket.tracking_active = True


def _positive_num(val: Any) -> Optional[Union[int, float]]:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return val
    return None


def _nonneg_num(val: Any) -> Optional[Union[int, float]]:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
        return val
    return None


# DALTON-HOOK(complete-parse-failure-billing): normalize raw provider usage payloads attached to
# SDK parse exceptions (e.g. OpenAI Responses/Chat Completions httpx.Response or LengthFinishReasonError).
def _normalize_raw_provider_usage(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        if hasattr(raw, "model_dump"):
            try:
                raw = raw.model_dump()
            except Exception:
                return {}
        elif hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                return {}
        elif hasattr(raw, "__dict__"):
            raw = vars(raw)
        else:
            return {}
    if not isinstance(raw, dict):
        return {}

    # DALTON-HOOK(complete-parse-failure-billing): normalize input/output/total token fields across SDKs.
    out: Dict[str, Any] = {}
    in_tok = _nonneg_num(raw.get("input_tokens"))
    if in_tok is None:
        in_tok = _nonneg_num(raw.get("prompt_tokens"))
    if in_tok is None:
        in_tok = _nonneg_num(raw.get("promptTokenCount"))

    out_tok = _nonneg_num(raw.get("output_tokens"))
    if out_tok is None:
        out_tok = _nonneg_num(raw.get("completion_tokens"))
    if out_tok is None:
        out_tok = _nonneg_num(raw.get("candidatesTokenCount"))

    tot_tok = _nonneg_num(raw.get("total_tokens"))
    if tot_tok is None:
        tot_tok = _nonneg_num(raw.get("totalTokenCount"))

    if in_tok is not None:
        out["input_tokens"] = in_tok
    if out_tok is not None:
        out["output_tokens"] = out_tok
    if tot_tok is not None:
        out["total_tokens"] = tot_tok
    elif (in_tok or 0) > 0 or (out_tok or 0) > 0:
        out["total_tokens"] = (in_tok or 0) + (out_tok or 0)

    # DALTON-HOOK(complete-parse-failure-billing): normalize cached input tokens across provider formats.
    cached = _positive_num(raw.get("cached_tokens"))
    for details_key, sub_key in (
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("input_token_details", "cache_read"),
    ):
        if cached is not None:
            break
        det = raw.get(details_key)
        if isinstance(det, dict):
            cached = _positive_num(det.get(sub_key))
    if cached is None:
        cached = _positive_num(raw.get("cache_read_input_tokens"))
    if cached is None:
        cached = _positive_num(raw.get("cachedContentTokenCount"))
    if cached is not None:
        out["cached_tokens"] = cached

    # DALTON-HOOK(complete-parse-failure-billing): normalize cache-creation, reasoning, and audio tokens.
    cache_5m = _positive_num(raw.get("cache_5m_tokens"))
    cache_1h = _positive_num(raw.get("cache_1h_tokens"))
    cc = raw.get("cache_creation")
    if isinstance(cc, dict):
        cache_5m = _positive_num(cc.get("ephemeral_5m_input_tokens")) or cache_5m
        cache_1h = _positive_num(cc.get("ephemeral_1h_input_tokens")) or cache_1h
    if cache_5m is None and cache_1h is None:
        cache_5m = _positive_num(raw.get("cache_creation_input_tokens"))
    if cache_5m is not None:
        out["cache_5m_tokens"] = cache_5m
    if cache_1h is not None:
        out["cache_1h_tokens"] = cache_1h

    # Reasoning & audio tokens
    reasoning = _positive_num(raw.get("reasoning_tokens"))
    for details_key, sub_key in (
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_token_details", "reasoning"),
    ):
        if reasoning is not None:
            break
        det = raw.get(details_key)
        if isinstance(det, dict):
            reasoning = _positive_num(det.get(sub_key))
    if reasoning is None:
        reasoning = _positive_num(raw.get("thoughtsTokenCount"))
    if reasoning is not None:
        out["reasoning_tokens"] = reasoning

    # DALTON-HOOK(complete-parse-failure-billing): normalize audio tokens and require positive reported usage.
    for details_key, sub_key, dest_key in (
        ("prompt_tokens_details", "audio_tokens", "audio_input_tokens"),
        ("completion_tokens_details", "audio_tokens", "audio_output_tokens"),
    ):
        det = raw.get(details_key)
        if isinstance(det, dict):
            audio_v = _positive_num(det.get(sub_key))
            if audio_v is not None:
                out[dest_key] = audio_v

    if not any((out.get(k) or 0) > 0 for k in _REPORTED_TOKEN_KEYS):
        return {}
    return out


# DALTON-HOOK(complete-parse-failure-billing): recover provider usage from *this* request's own
# SDK parse exception only (never walk __cause__/__context__ so a fallback/retry error cannot
# double-bill usage from an earlier upstream request).
def _extract_usage_from_sdk_exception(exc: BaseException) -> Dict[str, Any]:
    for attr in ("token_usage", "usage"):
        cand = _normalize_raw_provider_usage(getattr(exc, attr, None))
        if cand:
            return cand

    completion = getattr(exc, "completion", None)
    if completion is not None:
        cand = _normalize_raw_provider_usage(getattr(completion, "usage", None))
        if cand:
            return cand

    for attr in ("raw", "llm_output"):
        obj = getattr(exc, attr, None)
        if obj is not None:
            cand = _normalize_raw_provider_usage(extract_token_usage(obj))
            if cand:
                return cand

    # DALTON-HOOK(complete-parse-failure-billing): recover usage from attached HTTP/SDK response body.
    resp = getattr(exc, "response", None)
    if resp is not None:
        if hasattr(resp, "usage_metadata") or hasattr(resp, "response_metadata"):
            cand = _normalize_raw_provider_usage(extract_token_usage(resp))
            if cand:
                return cand
        elif isinstance(resp, dict):
            cand = _normalize_raw_provider_usage(
                resp.get("usage") or resp.get("usageMetadata") or resp
            )
            if cand:
                return cand
        elif callable(getattr(resp, "json", None)):
            status_code = getattr(resp, "status_code", None)
            if not isinstance(status_code, int) or 200 <= status_code < 300:
                try:
                    body = resp.json()
                except Exception:
                    body = None
                if isinstance(body, dict):
                    raw_u = body.get("usage") or body.get("usageMetadata")
                    if raw_u is None and isinstance(body.get("response"), dict):
                        raw_u = body["response"].get("usage")
                    cand = _normalize_raw_provider_usage(raw_u)
                    if cand:
                        return cand

    return {}


# DALTON-HOOK(complete-parse-failure-billing): align unbilled pre-stream error detection with
# provider classification (402/billing only when stream never started, invalid_api_key, text 4xx).
def _exc_stream_started(exc: BaseException) -> bool:
    """Return True if the exception (or its causal chain) indicates stream output had already started."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "stream_started", None) is True:
            return True
        raw_chunks = getattr(cur, "chunks_received", None)
        if isinstance(raw_chunks, int) and not isinstance(raw_chunks, bool) and raw_chunks > 0:
            return True
        if getattr(cur, "mid_stream", False) is True:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _extract_structured_err_code(exc: BaseException) -> Optional[str]:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        top = body.get("code")
        if isinstance(top, str) and top:
            return top
        err_obj = body.get("error")
        if isinstance(err_obj, dict):
            nested = err_obj.get("code")
            if isinstance(nested, str) and nested:
                return nested
    return None


# DALTON-HOOK(complete-parse-failure-billing): classify deterministic client/pre-billing SDK errors.
def _is_unbilled_sdk_error(exc: BaseException) -> bool:
    """Return True for client-side/pre-billing rejections that never incur upstream charges."""
    if _exc_stream_started(exc):
        return False
    if isinstance(exc, (NotImplementedError, AttributeError, TypeError, KeyError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None)
    if isinstance(status_code, int) and status_code in (400, 401, 402, 403, 404, 422, 426):
        return True
    if _extract_structured_err_code(exc) == "invalid_api_key":
        return True
    cls_name = exc.__class__.__name__
    if cls_name in ("MissingProviderKeyError", "ProviderDisabledError", "ValueError"):
        return True
    # DALTON-HOOK(complete-parse-failure-billing): a pydantic ValidationError echoes response field
    # values in its text, so it never matches the text markers below and stays unknown usage.
    if isinstance(exc, PydanticValidationError):
        return False
    # DALTON-HOOK(complete-parse-failure-billing): match pre-send/billing/4xx text markers when no status_code is set.
    msg_lower = str(exc).lower()
    if any(
        token in msg_lower
        for token in (
            "missingproviderkey",
            "providerdisabled",
            "no model configured",
            "not logged in",
            "no valid oauth token",
            "oauth_required",
            "requires a connected",
            "unsupported sdk",
            "used all credits",
            "monthly spending limit",
            "need a grok subscription",
            "usage balance exhausted",
        )
    ):
        return True
    for code in ("400", "401", "402", "403", "404", "422"):
        if (
            f"http {code}" in msg_lower
            or f"status {code}" in msg_lower
            or f"status_code={code}" in msg_lower
            or f"error code: {code}" in msg_lower
        ):
            return True
    return False


# DALTON-HOOK(complete-parse-failure-billing): record completed call usage into the active bucket and call record.
def _record_collected_usage(
    token_info: Any,
    *,
    capture: Optional[Dict[str, Any]] = None,
) -> None:
    bucket = _USAGE_ACCUMULATOR.get()
    if not isinstance(token_info, dict):
        return
    numeric = {
        k: v
        for k, v in token_info.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if any(numeric.get(k, 0) > 0 for k in _REPORTED_TOKEN_KEYS):
        if capture is not None:
            rec = capture.get("record")
            if isinstance(rec, dict):
                rec["usage"] = dict(numeric)
        if bucket is not None:
            bucket.append(numeric)


# DALTON-HOOK(complete-parse-failure-billing): track each upstream ainvoke call's send timestamp and outcome.
@contextlib.contextmanager
def _track_upstream_call():
    """Track an active upstream `ainvoke` call, timestamping when it is sent so cross-day
    attribution uses send time and timeouts during retry backoff sleep are distinguished from
    in-flight provider requests."""
    bucket = _USAGE_ACCUMULATOR.get()
    sent_at = _utc_now()
    record: Dict[str, Any] = {
        "occurred_at": sent_at,
        "usage": {},
        "usage_known": True,
        "unknown_usage_calls": 0,
        "outcome": "in_flight",
    }
    capture: Dict[str, Any] = {"usage": {}, "occurred_at": sent_at, "record": record}
    if bucket is not None:
        bucket.tracking_active = True
        bucket.sent_calls += 1
        bucket.in_flight_calls += 1
        bucket.call_records.append(record)
    try:
        yield capture
    except BaseException as exc:
        # DALTON-HOOK(complete-parse-failure-billing): recover usage from this request's own exception or classify unbilled vs unknown.
        extracted: Dict[str, Any] = {}
        if isinstance(exc, Exception):
            extracted = _extract_usage_from_sdk_exception(exc)
        if extracted:
            capture["usage"] = dict(extracted)
            record["usage"] = dict(extracted)
            record["outcome"] = "sdk_usage_error"
            record["usage_known"] = True
            record["unknown_usage_calls"] = 0
            if bucket is not None:
                bucket.append(dict(extracted))
        if bucket is not None:
            bucket.in_flight_calls = max(0, bucket.in_flight_calls - 1)
        if not extracted:
            is_unbilled = False
            if not isinstance(exc, (asyncio.CancelledError, TimeoutError)):
                # A ValidationError without usage stays unknown: LangChain can raise it while
                # building the message from a response the provider already billed.
                is_unbilled = _is_unbilled_sdk_error(exc)
                if not is_unbilled and bucket is not None and callable(bucket.is_unbilled_error):
                    try:
                        is_unbilled = bool(bucket.is_unbilled_error(exc))
                    except Exception:
                        is_unbilled = False
            # DALTON-HOOK(complete-parse-failure-billing): stamp unbilled_error vs unknown_error on this call record.
            if is_unbilled:
                record["outcome"] = "unbilled_error"
                record["usage_known"] = True
                record["unknown_usage_calls"] = 0
            else:
                record["outcome"] = "unknown_error"
                record["usage_known"] = False
                record["unknown_usage_calls"] = 1
                if bucket is not None:
                    bucket.unknown_calls += 1
        raise
    else:
        record["outcome"] = "completed"
        if bucket is not None:
            bucket.in_flight_calls = max(0, bucket.in_flight_calls - 1)


def create_messages(system_prompt: str, user_prompt: str) -> list:
    """Create a list of messages for LLM invocation.

    Note: This returns messages directly without using template.invoke()
    to avoid creating a separate LangSmith trace for prompt rendering.
    """
    from langchain_core.messages import SystemMessage, HumanMessage
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]


def maybe_disable_streaming(model: object) -> None:
    """Set ``streaming=False`` unless the provider rejects non-streaming.

    The Codex proxy requires ``stream=true`` on every request (it returns
    HTTP 400 ``{'detail': 'Stream must be set to true'}`` otherwise), so
    ``ChatCodexOpenAI`` instances must keep their construction-time
    ``streaming=True`` flag. Mutates ``model`` in place — callers that
    share the instance across requests MUST deep-copy first (see
    ``thread_maintenance.py::compact`` and ``fetch.py::_extract_with_llm``
    for the reference pattern).

    The ``RunnableBindingBase`` unwrap is defensive — keeps the Codex guard
    and streaming flip working even if a caller wraps via ``bind()``.
    """
    from langchain_core.runnables.base import RunnableBindingBase
    from src.llms.extension.codex import ChatCodexOpenAI

    target = model.bound if isinstance(model, RunnableBindingBase) else model

    if isinstance(target, ChatCodexOpenAI):
        return
    if hasattr(target, "streaming"):
        target.streaming = False


async def make_api_call(
    llm: object,
    system_prompt: str,
    user_prompt: str,
    response_schema: Optional[Type[BaseModel]] = None,
    return_token_usage: bool = False,
    max_parsing_retries: int = 5,
    disable_tracing: bool = False,
) -> Union[Dict[str, Any], BaseModel, str, Tuple[Any, Dict[str, Any]]]:
    """
    Make an async API call to an LLM with flexible response handling.

    Args:
        llm: The LLM instance
        system_prompt: System prompt
        user_prompt: User prompt
        response_schema: Optional Pydantic schema for structured output
        return_token_usage: If True, returns tuple of (response, token_usage)
        max_parsing_retries: Maximum number of retries for parsing failures (default: 5)
        disable_tracing: If True, disables LangSmith tracing for this call

    Returns:
        Structured response, dict, or string depending on configuration
        If return_token_usage=True, returns tuple of (response, token_usage)

    Note:
        When a model doesn't support native structured output, the system will retry
        parsing up to max_parsing_retries times before failing.
    """
    from langsmith import tracing_context

    # Enable stream_usage for OpenAI models to get token counts
    if hasattr(llm, 'model_name') and hasattr(llm, 'stream_usage'):
        llm.stream_usage = True

    messages = create_messages(system_prompt, user_prompt)

    # Only ever switch tracing off: enabled=True would turn it on for a
    # deployment that never configured LangSmith.
    # DALTON-HOOK(complete-parse-failure-billing): mark collector active upon entering make_api_call.
    mark_api_call_tracking_active()
    with tracing_context(enabled=False) if disable_tracing else contextlib.nullcontext():
        # Case 1: No schema requested, return raw content
        if response_schema is None:
            # DALTON-HOOK(complete-parse-failure-billing): track active upstream call for accurate unknown-usage accounting.
            with _track_upstream_call() as raw_capture:
                response = await llm.ainvoke(messages)
            content = get_message_content(response)

            # Extract token usage (silent by default)
            token_info = extract_token_usage(response)
            # DALTON-HOOK(complete-parse-failure-billing): record completed upstream call usage.
            _record_collected_usage(token_info, capture=raw_capture)

            if return_token_usage:
                return content, token_info
            return content

        # Case 2: Native structured output
        # DALTON-HOOK(notional-pricing): retain raw usage when native structured output
        # returns a raw response whose parsed field is None, and record per-attempt
        # usage dicts (`per_call_usages`) across fallback retries so threshold-priced
        # models evaluate each request's input length individually.
        raw_token_info: Dict[str, Any] = {}
        # DALTON-HOOK(complete-parse-failure-billing): capture usage from native SDK parse exceptions
        # (e.g. OpenAI Responses/Chat Completions returning usage on truncated structured output),
        # and exit the native except handler before issuing fallback calls so fallback exceptions
        # never inherit the native exception via __context__.
        native_captures: list[Dict[str, Any]] = []
        native_failed = False
        try:
            # Use include_raw=True to get token usage with structured output
            client = llm.with_structured_output(response_schema, include_raw=True)
            with _track_upstream_call() as native_capture:
                native_captures.append(native_capture)
                response_with_raw = await client.ainvoke(messages)

            token_info = {}

            # Extract the parsed response and raw response
            # Check if response_with_raw is not None before checking if it's a dict
            if response_with_raw is not None and isinstance(response_with_raw, dict) and 'raw' in response_with_raw:
                raw_response = response_with_raw['raw']
                if raw_response is not None:
                    token_info = extract_token_usage(raw_response)
                    # DALTON-HOOK(complete-parse-failure-billing): record native raw call usage even if parsed is None.
                    _record_collected_usage(token_info, capture=native_capture)
                    if token_info:
                        raw_token_info = {
                            k: v
                            for k, v in token_info.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool)
                        }
                response = response_with_raw.get('parsed')
                if response is None:
                    # Parsing failed, fall back to manual parsing outside exception handler
                    native_failed = True
            else:
                # Fallback to direct response if include_raw didn't work
                # DALTON-HOOK(complete-parse-failure-billing): track direct structured-output upstream call.
                client = llm.with_structured_output(response_schema)
                with _track_upstream_call() as direct_capture:
                    native_captures.append(direct_capture)
                    response = await client.ainvoke(messages)

            if not native_failed:
                if return_token_usage:
                    return response, token_info
                return response

        except Exception:
            native_failed = True

        # Fallback: Model doesn't support structured output or native parsing failed;
        # run manual parsing retries outside the native except handler so no prior exception
        # is chained onto fallback errors via `__context__`.
        from pydantic import ValidationError

        last_error = None
        total_token_info = {}
        per_call_usages: list[Dict[str, Any]] = []
        # DALTON-HOOK(complete-parse-failure-billing): carry usage recovered from native SDK
        # parse failures into fallback totals and per_call_usages without double-counting.
        native_sdk_usages = [
            dict(c["usage"]) for c in native_captures if isinstance(c.get("usage"), dict) and c["usage"]
        ]
        if raw_token_info:
            for key, value in raw_token_info.items():
                total_token_info[key] = total_token_info.get(key, 0) + value
            per_call_usages.append(dict(raw_token_info))
        elif native_sdk_usages:
            for sdk_usage in native_sdk_usages:
                for key, value in sdk_usage.items():
                    total_token_info[key] = total_token_info.get(key, 0) + value
                per_call_usages.append(dict(sdk_usage))
        retry_count = 0

        for attempt in range(max_parsing_retries):
            retry_count = attempt + 1
            fb_capture: Dict[str, Any] = {"usage": {}}
            # DALTON-HOOK(complete-parse-failure-billing): isolate `llm.ainvoke` from response parsing so
            # a ValueError/ValidationError without usage re-raises at once instead of entering parsing
            # retries (its call record decides billed vs unknown); only errors with recovered usage retry.
            try:
                with _track_upstream_call() as fb_capture:
                    response = await llm.ainvoke(messages)
            except (json.JSONDecodeError, ValidationError, ValueError) as invoke_parse_err:
                if fb_capture.get("usage"):
                    for key, value in fb_capture["usage"].items():
                        total_token_info[key] = total_token_info.get(key, 0) + value
                    per_call_usages.append(dict(fb_capture["usage"]))
                    last_error = invoke_parse_err
                    if retry_count < max_parsing_retries:
                        messages = create_messages(
                            system_prompt + f"\n\nIMPORTANT: Your response MUST be valid JSON that matches this schema: {response_schema.model_json_schema()}",
                            user_prompt + f"\n\n(Retry {retry_count + 1}/{max_parsing_retries} due to parsing error. Please ensure your response is properly formatted JSON.)"
                        )
                        await asyncio.sleep(0.5 * retry_count)  # Exponential backoff
                        continue
                    break
                # DALTON-HOOK(complete-parse-failure-billing): attach prior call usages before re-raising unbilled client error.
                if per_call_usages and getattr(invoke_parse_err, "token_usage", None) is None:
                    partial_usage = dict(total_token_info)
                    partial_usage["retry_attempts"] = retry_count
                    partial_usage["per_call_usages"] = [dict(cu) for cu in per_call_usages]
                    try:
                        setattr(invoke_parse_err, "token_usage", partial_usage)
                    except Exception:
                        pass
                raise
            except Exception as retry_exc:
                # DALTON-HOOK(complete-parse-failure-billing): preserve usage from earlier
                # completed calls if a subsequent retry aborts with timeout/5xx/stream cut.
                if fb_capture.get("usage"):
                    for key, value in fb_capture["usage"].items():
                        total_token_info[key] = total_token_info.get(key, 0) + value
                    per_call_usages.append(dict(fb_capture["usage"]))
                if per_call_usages and getattr(retry_exc, "token_usage", None) is None:
                    partial_usage = dict(total_token_info)
                    partial_usage["retry_attempts"] = retry_count
                    partial_usage["per_call_usages"] = [dict(cu) for cu in per_call_usages]
                    try:
                        setattr(retry_exc, "token_usage", partial_usage)
                    except Exception:
                        pass
                raise

            try:
                content = get_message_content(response)

                # Extract and accumulate token usage
                # DALTON-HOOK(notional-pricing): accumulate scalar token counts and
                # append each real response's usage to `per_call_usages`.
                token_info = extract_token_usage(response)
                # DALTON-HOOK(complete-parse-failure-billing): record each fallback call's usage as it arrives.
                _record_collected_usage(token_info, capture=fb_capture)
                if token_info:
                    call_usage: Dict[str, Any] = {}
                    for key, value in token_info.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            total_token_info[key] = total_token_info.get(key, 0) + value
                            call_usage[key] = value
                    if call_usage:
                        per_call_usages.append(call_usage)

                # Flatten content_blocks (reasoning + text) to text.
                if isinstance(content, list):
                    content = format_llm_content(content).get("text", "") or ""

                # Try to extract and parse JSON
                if isinstance(content, str):
                    # Look for JSON structure in the content
                    # Try multiple extraction strategies
                    json_str = None

                    # Strategy 1: Find complete JSON object
                    start_idx = content.find('{')
                    end_idx = content.rfind('}') + 1
                    if start_idx != -1 and end_idx > start_idx:
                        json_str = content[start_idx:end_idx]

                    # Strategy 2: Look for code blocks with JSON
                    if not json_str:
                        json_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
                        import re
                        match = re.search(json_pattern, content, re.DOTALL)
                        if match:
                            json_str = match.group(1)

                    if json_str:
                        # Parse JSON and validate with schema
                        data = json.loads(json_str)
                        parsed = response_schema(**data)

                        # Add retry metadata to token info
                        # DALTON-HOOK(notional-pricing): attach per_call_usages for per-attempt threshold pricing.
                        total_token_info['retry_attempts'] = retry_count
                        if per_call_usages:
                            total_token_info['per_call_usages'] = per_call_usages

                        if return_token_usage:
                            return parsed, total_token_info
                        return parsed
                    else:
                        raise ValueError(f"No JSON structure found in response (attempt {retry_count}/{max_parsing_retries})")
                else:
                    raise ValueError(f"Response is not a string (attempt {retry_count}/{max_parsing_retries})")

            except (json.JSONDecodeError, ValidationError, ValueError) as parse_error:
                last_error = parse_error
                if retry_count < max_parsing_retries:
                    # Add clarification to the prompt for next attempt
                    messages = create_messages(
                        system_prompt + f"\n\nIMPORTANT: Your response MUST be valid JSON that matches this schema: {response_schema.model_json_schema()}",
                        user_prompt + f"\n\n(Retry {retry_count + 1}/{max_parsing_retries} due to parsing error. Please ensure your response is properly formatted JSON.)"
                    )
                    await asyncio.sleep(0.5 * retry_count)  # Exponential backoff
                    continue

        # All retries failed, raise the last error with context
        # DALTON-HOOK(notional-pricing): retain per_call_usages on exhausted parsing retries.
        total_token_info['retry_attempts'] = retry_count
        if per_call_usages:
            total_token_info['per_call_usages'] = per_call_usages
        total_token_info['parsing_failed'] = True

        # DALTON-HOOK(workspace-dedup): redact raw parse error details when caller requests log redaction.
        if _REDACT_PARSE_ERROR_LOGS.get():
            error_msg = f"Failed to parse response after {retry_count} attempts. Last error: {type(last_error).__name__}"
        else:
            error_msg = f"Failed to parse response after {retry_count} attempts. Last error: {last_error}"
        logger.debug(error_msg)  # Changed from error to debug to reduce console noise

        # DALTON-HOOK(complete-parse-failure-billing): raise StructuredOutputParseError (ValueError
        # subclass) carrying `token_usage` so journal billing records all completed upstream calls.
        if return_token_usage:
            raise StructuredOutputParseError(
                error_msg + f" | Token usage: {total_token_info}",
                token_usage=total_token_info,
                retry_attempts=retry_count,
            )
        else:
            raise StructuredOutputParseError(
                error_msg,
                token_usage=total_token_info,
                retry_attempts=retry_count,
            )
    

async def parse_structured_output(llm: object, text: str, schema_class: Optional[Type[BaseModel]] = None) -> Union[BaseModel, Dict[str, Any]]:
    """
    Asynchronously parse unstructured text into a structured format using an LLM.
    
    Args:
        llm: The LLM instance to use for parsing
        text: The text to parse
        schema_class: The Pydantic schema class to parse into
        
    Returns:
        Parsed structured output as a Pydantic model instance
    """
    if schema_class is None:
        raise ValueError("schema_class parameter is required for parse_structured_output")
    
    system_prompt = f"You are a helpful assistant that understands and translates text to JSON format according to the following schema. {schema_class.model_json_schema()}"
    user_prompt = f"{text}"
    messages = create_messages(system_prompt, user_prompt)

    try:
        client = llm.with_structured_output(schema_class)
        response = await client.ainvoke(messages)
        return response
    except Exception as e:
        # If structured output fails, try to parse manually
        response = await llm.ainvoke(messages)
        content = get_message_content(response)
        # Try to parse JSON manually
        import json
        try:
            data = json.loads(content)
            return schema_class(**data)
        except Exception:
            raise e
    
