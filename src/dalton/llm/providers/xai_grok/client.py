"""Dalton-owned xAI Grok Responses API client (`cli-chat-proxy.grok.com/v1/responses`)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import (
    agenerate_from_stream,
    generate_from_stream,
)
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

import src.llms.extension.codex  # noqa: F401 — ensure null-output guard is installed
from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_dalton_meta, load_overlay_data
from src.dalton.llm.providers.xai_grok.oauth import (
    XAI_PROVIDER,
    XAI_PROXY_BASE_URL,
)
from src.dalton.settings import get_xai_grok_client_version
from src.llms.extension.dashscope import ScopedResponsesStream, _codex_pending_calls

_XAI_BILLING_MARKERS: tuple[str, ...] = (
    "used all credits",
    "monthly spending limit",
    "need a grok subscription",
    # 402 from cli-chat-proxy.grok.com, seen 1,922× on 10/30: the account's
    # Grok Build balance is gone until it is paid, not a transient refusal.
    "usage balance exhausted",
)

_XAI_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit exceeded",
    "too many requests",
)


def _iter_exc_chain(err: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def mark_xai_exception(
    exc: BaseException,
    *,
    stream_started: bool | None = None,
    chunks_received: int | None = None,
) -> BaseException:
    """Stamp an exception raised by the Grok client so error classification only applies to xAI."""
    try:
        setattr(exc, "_xai_grok", True)
        if not getattr(exc, "llm_provider", None):
            setattr(exc, "llm_provider", XAI_PROVIDER)
        if not getattr(exc, "llm_sdk", None):
            setattr(exc, "llm_sdk", "grok")
        if stream_started is not None:
            setattr(exc, "stream_started", bool(stream_started))
            if stream_started:
                setattr(exc, "mid_stream", True)
        if chunks_received is not None and getattr(exc, "chunks_received", None) is None:
            setattr(exc, "chunks_received", int(chunks_received))
    except Exception:
        pass
    return exc


def is_xai_exception(
    err: BaseException | str | None,
    *,
    provider: str | None = None,
    sdk: str | None = None,
) -> bool:
    """Return True only when the error is confirmed to come from `xai-oauth` or sdk `grok`."""
    if provider == XAI_PROVIDER or sdk == "grok":
        return True
    if not isinstance(err, BaseException):
        return False
    for cur in _iter_exc_chain(err):
        if getattr(cur, "_xai_grok", False) is True:
            return True
        if getattr(cur, "llm_provider", None) == XAI_PROVIDER or getattr(cur, "provider", None) == XAI_PROVIDER:
            return True
        if getattr(cur, "llm_sdk", None) == "grok" or getattr(cur, "sdk", None) == "grok":
            return True
        req = getattr(cur, "request", None) or getattr(getattr(cur, "response", None), "request", None)
        if req is not None:
            url_str = str(getattr(req, "url", "") or "").lower()
            if "cli-chat-proxy.grok.com" in url_str:
                return True
            headers = getattr(req, "headers", None) or {}
            if hasattr(headers, "get") and headers.get("X-XAI-Token-Auth") == "xai-grok-cli":
                return True
    return False


def get_stream_progress(exc: BaseException | None) -> tuple[bool, int]:
    """Return `(stream_started, chunks_received)` from `exc`'s causal chain.

    If `mark_xai_exception` wrote an explicit `stream_started` or
    `chunks_received` anywhere on the chain, those explicit values govern;
    `mid_stream` is consulted only when neither explicit attribute is present.
    """
    if exc is None:
        return False, 0
    has_explicit = False
    explicit_started = False
    max_chunks = 0
    mid_stream = False
    for cur in _iter_exc_chain(exc):
        raw_started = getattr(cur, "stream_started", None)
        if isinstance(raw_started, bool):
            has_explicit = True
            if raw_started:
                explicit_started = True
        raw_chunks = getattr(cur, "chunks_received", None)
        if isinstance(raw_chunks, int) and not isinstance(raw_chunks, bool):
            has_explicit = True
            if raw_chunks > max_chunks:
                max_chunks = raw_chunks
        if getattr(cur, "mid_stream", False) is True:
            mid_stream = True
    if has_explicit:
        return (explicit_started or max_chunks > 0), max_chunks
    return mid_stream, 0


def has_stream_started(exc: BaseException | None) -> bool:
    """Return True if the stream had already yielded output before `exc` was raised."""
    started, _ = get_stream_progress(exc)
    return started


def classify_xai_error(
    err: BaseException | str | None,
    *,
    provider: str | None = None,
    sdk: str | None = None,
    require_xai_origin: bool = False,
) -> str | None:
    """Classify xAI error prose into `'billing'`, `'rate_limit'`, or `None`."""
    if err is None:
        return None
    if require_xai_origin and not is_xai_exception(err, provider=provider, sdk=sdk):
        return None
    if isinstance(err, BaseException):
        parts: list[str] = []
        for cur in _iter_exc_chain(err):
            parts.append(str(cur))
            body = getattr(cur, "body", None)
            if body:
                parts.append(str(body))
            resp = getattr(cur, "response", None)
            if resp is not None:
                try:
                    parts.append(str(getattr(resp, "text", "") or ""))
                except Exception:
                    pass
        text = " ".join(parts).lower()
    else:
        text = str(err).lower()

    if any(marker in text for marker in _XAI_BILLING_MARKERS):
        return "billing"
    if any(marker in text for marker in _XAI_RATE_LIMIT_MARKERS):
        return "rate_limit"
    return None


def resolve_xai_supports_reasoning_effort(
    model_name: str | None,
    *,
    explicit: bool | None = None,
    profile: Any = None,
) -> bool:
    """Resolve whether a Grok model supports `reasoning.effort` from explicit params or `native_overlay.json`."""
    if explicit is not None:
        return bool(explicit)
    if isinstance(profile, dict) and "supports_reasoning_effort" in profile:
        return bool(profile["supports_reasoning_effort"])

    key = str(model_name or "").strip()
    if not key:
        return True

    candidates = [key]
    if key.endswith("-oauth"):
        candidates.append(key[: -len("-oauth")])
    else:
        candidates.append(f"{key}-oauth")

    try:
        overlay = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
        models_map = overlay.get("models") or {}
        for cand in candidates:
            m_entry = models_map.get(cand)
            if isinstance(m_entry, dict):
                if "supports_reasoning_effort" in m_entry:
                    return bool(m_entry["supports_reasoning_effort"])
                params = m_entry.get("parameters")
                if isinstance(params, dict) and "supports_reasoning_effort" in params:
                    return bool(params["supports_reasoning_effort"])
        meta_map = load_dalton_meta() or {}
        for cand in candidates:
            meta_entry = meta_map.get(cand)
            if isinstance(meta_entry, dict) and "supports_reasoning_effort" in meta_entry:
                return bool(meta_entry["supports_reasoning_effort"])
    except Exception:
        pass

    return True


def _normalize_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Normalize an image block from a tool output array for a follow-up user message."""
    btype = block.get("type")
    if btype == "input_image":
        return dict(block)
    if btype == "image_url":
        img_val = block.get("image_url")
        if isinstance(img_val, dict):
            url = img_val.get("url") or ""
        else:
            url = str(img_val or "")
        return {"type": "input_image", "image_url": url}
    if btype == "image":
        url = block.get("url") or block.get("image_url") or block.get("data") or ""
        return {"type": "input_image", "image_url": str(url)}
    return dict(block)


def flatten_tool_outputs_in_input(items: list[Any]) -> list[Any]:
    """Flatten array `output` in `function_call_output` items to text and move images to a subsequent user message."""
    out: list[Any] = []
    for item in items:
        if not (isinstance(item, dict) and item.get("type") == "function_call_output"):
            out.append(item)
            continue

        raw_output = item.get("output")
        if not isinstance(raw_output, list):
            out.append(item)
            continue

        text_parts: list[str] = []
        image_blocks: list[dict[str, Any]] = []

        for elem in raw_output:
            if isinstance(elem, str):
                text_parts.append(elem)
            elif isinstance(elem, dict):
                etype = elem.get("type")
                if etype in ("input_text", "text", "output_text"):
                    text_parts.append(str(elem.get("text") or ""))
                elif etype in ("input_image", "image_url", "image"):
                    image_blocks.append(_normalize_image_block(elem))
                elif "text" in elem:
                    text_parts.append(str(elem.get("text") or ""))
                else:
                    text_parts.append(json.dumps(elem, ensure_ascii=False))
            elif elem is not None:
                text_parts.append(str(elem))

        flattened_item = {
            **item,
            "output": "\n".join(p for p in text_parts if p),
        }
        out.append(flattened_item)

        if image_blocks:
            out.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": image_blocks,
                }
            )
    return out


class ChatGrokOpenAI(ScopedResponsesStream):
    """Responses API client for `https://cli-chat-proxy.grok.com/v1/responses`."""

    supports_reasoning_effort: bool = True
    is_reasoning_model: bool = True
    session_id: str | None = None
    grok_client_version: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.output_version:
            self.output_version = "responses/v1"
        if not self.openai_api_base:
            self.openai_api_base = XAI_PROXY_BASE_URL
        self.streaming = True

        mk = dict(self.model_kwargs or {})
        eff_session_id = (
            self.session_id
            or mk.get("prompt_cache_key")
            or str(uuid.uuid4())
        )
        self.session_id = str(eff_session_id)
        mk.setdefault("prompt_cache_key", self.session_id)
        self.model_kwargs = mk

        ver = self.grok_client_version or get_xai_grok_client_version()
        headers = dict(self.default_headers or {})
        raw_key = ""
        if self.openai_api_key is not None:
            get_secret = getattr(self.openai_api_key, "get_secret_value", None)
            raw_key = get_secret() if callable(get_secret) else str(self.openai_api_key)
        if raw_key and "Authorization" not in headers and "authorization" not in headers:
            headers["Authorization"] = f"Bearer {raw_key}"
        headers.setdefault("X-XAI-Token-Auth", "xai-grok-cli")
        headers.setdefault("x-grok-client-version", ver)
        headers.setdefault("x-grok-model-override", str(self.model))
        headers.setdefault("x-client-request-id", self.session_id)
        self.default_headers = headers

        super().model_post_init(__context)

    def _stream_scope(self):
        return _codex_pending_calls, {}

    def _stream(self, *args: Any, **kwargs: Any):
        chunks_yielded = 0
        try:
            for chunk in super()._stream(*args, **kwargs):
                chunks_yielded += 1
                yield chunk
        except BaseException as exc:
            mark_xai_exception(
                exc,
                stream_started=chunks_yielded > 0,
                chunks_received=chunks_yielded,
            )
            raise

    async def _astream(self, *args: Any, **kwargs: Any):
        chunks_yielded = 0
        try:
            async for chunk in super()._astream(*args, **kwargs):
                chunks_yielded += 1
                yield chunk
        except BaseException as exc:
            mark_xai_exception(
                exc,
                stream_started=chunks_yielded > 0,
                chunks_received=chunks_yielded,
            )
            raise

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            stream_iter = self._stream(messages, stop=stop, **kwargs)
            return generate_from_stream(stream_iter)
        except BaseException as exc:
            mark_xai_exception(exc)
            raise

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            stream_iter = self._astream(messages, stop=stop, **kwargs)
            return await agenerate_from_stream(stream_iter)
        except BaseException as exc:
            mark_xai_exception(exc)
            raise

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload["stream"] = True
        payload["store"] = False

        if self.session_id and not payload.get("prompt_cache_key"):
            payload["prompt_cache_key"] = self.session_id

        kw_supports = payload.pop("supports_reasoning_effort", None)
        explicit_flag = (
            bool(kw_supports)
            if kw_supports is not None
            else (False if self.supports_reasoning_effort is False else None)
        )
        supports_effort = resolve_xai_supports_reasoning_effort(
            str(self.model),
            explicit=explicit_flag,
            profile=self.profile,
        )

        if not supports_effort:
            payload.pop("reasoning", None)
        else:
            existing_reasoning = payload.get("reasoning")
            if isinstance(existing_reasoning, dict):
                r_dict = dict(existing_reasoning)
                if self.reasoning_effort and "effort" not in r_dict:
                    r_dict["effort"] = self.reasoning_effort
                r_dict.setdefault("summary", "auto")
                payload["reasoning"] = r_dict
            elif self.reasoning_effort:
                payload["reasoning"] = {
                    "effort": self.reasoning_effort,
                    "summary": "auto",
                }

        is_reasoning = bool(
            (self.is_reasoning_model and supports_effort) or payload.get("reasoning")
        )
        if is_reasoning:
            inc = list(payload.get("include") or [])
            if "reasoning.encrypted_content" not in inc:
                inc.append("reasoning.encrypted_content")
            payload["include"] = inc
        elif isinstance(payload.get("include"), list):
            filtered_inc = [
                item for item in payload["include"] if item != "reasoning.encrypted_content"
            ]
            if filtered_inc:
                payload["include"] = filtered_inc
            else:
                payload.pop("include", None)

        if isinstance(payload.get("input"), list):
            payload["input"] = flatten_tool_outputs_in_input(payload["input"])

        return payload


def build_grok_client(llm: Any, cache_key: str | None = None) -> ChatGrokOpenAI:
    """Build native xAI Grok OAuth LLM client using `ChatGrokOpenAI`."""
    from src.llms.llm import STREAM_CHUNK_TIMEOUT_S, _merged_default_headers

    api_key = llm._resolve_api_key()
    session_id = str(cache_key) if cache_key else str(uuid.uuid4())
    client_version = get_xai_grok_client_version()

    params: dict[str, Any] = {
        "model": llm.model,
        "api_key": api_key,
        "streaming": True,
        "stream_usage": True,
        "output_version": "responses/v1",
        "max_retries": 5,
        "timeout": 600.0,
        "stream_chunk_timeout": STREAM_CHUNK_TIMEOUT_S,
        "session_id": session_id,
        "grok_client_version": client_version,
    }
    base_url_dict = llm._resolve_base_url("base_url")
    if base_url_dict:
        params.update(base_url_dict)
    else:
        params["base_url"] = XAI_PROXY_BASE_URL

    raw_params = dict(llm.parameters or {})
    explicit_supports = raw_params.pop("supports_reasoning_effort", None)
    supports_effort = resolve_xai_supports_reasoning_effort(
        getattr(llm, "custom_model_name", None) or llm.model,
        explicit=explicit_supports,
        profile=getattr(llm, "profile_overrides", None),
    )
    params["supports_reasoning_effort"] = supports_effort
    if not supports_effort:
        params["is_reasoning_model"] = False
        raw_params.pop("reasoning", None)

    params.update(raw_params)

    params["default_headers"] = _merged_default_headers(
        params,
        {
            "Authorization": f"Bearer {api_key}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": client_version,
            "x-grok-model-override": str(llm.model),
            "x-client-request-id": session_id,
        },
        llm.default_headers,
    )

    if llm.extra_body:
        params["extra_body"] = llm.extra_body

    existing_mk = dict(params.get("model_kwargs") or {})
    existing_mk["prompt_cache_key"] = session_id
    params["model_kwargs"] = existing_mk

    return ChatGrokOpenAI(**params)
