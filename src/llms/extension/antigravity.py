"""Native Google Antigravity chat model (subscription OAuth).

Speaks the Code Assist ``v1internal`` envelope protocol the official `agy`
CLI uses, reverse-engineered from CLI 1.4.2 traffic:

- ``POST {base_url}/v1internal:streamGenerateContent?alt=sse``
- Bearer auth with the Google OAuth access token; a User-Agent naming the
  CLI client is REQUIRED — without it the backend answers 403
  SUBSCRIPTION_REQUIRED ("no valid license").
- Request body is an envelope around the standard Vertex/Gemini
  GenerateContent request::

      {
        "project": "aicode-consumers",          # consumer-tier project id
        "model":   "gemini-4-argon-high",       # TOP-LEVEL, not inside request
        "requestId": "agent/<uuid>/<ms>/<uuid>/1",
        "userAgent": "antigravity",             # body-level client id
        "requestType": "agent",
        "request": { ...standard GenerateContentRequest... }
      }

- Responses are SSE frames of ``{"response": <GenerateContentResponse>,
  "traceId": ...}``.

The subscription's catalog includes non-Gemini models (Claude Opus/Sonnet
5.5, GPT-OSS 120B) behind the same endpoint and auth; they respond in the
same Gemini wire shape.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

import httpx
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import OutputTokenDetails, UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)

# The backend gates model access on this User-Agent (verified: without it the
# call 403s "no valid license"; with it the same call succeeds). Keep the
# aidev_client token and cl= build number — they are what production checks.
ANTIGRAVITY_USER_AGENT = (
    "antigravity/cli/1.4.2 (aidev_client; os_type=darwin; arch=arm64; "
    "cl=1001591392; auth_method=consumer)"
)
ANTIGRAVITY_BODY_USER_AGENT = "antigravity"
ANTIGRAVITY_DEFAULT_BASE_URL = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_DEFAULT_PROJECT = "aicode-consumers"

_EFFORT_SUFFIXES = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}

_THOUGHT_SIGNATURES_KEY = "antigravity_thought_signatures"


class AntigravityApiError(RuntimeError):
    """Upstream Antigravity call failed. ``status`` carries the HTTP code."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _normalize_thinking_level(value: Any) -> Optional[str]:
    """Accept either casing (LangAlpha writes lowercase efforts via
    ``reasoning.write: parameters.thinking_level``) → Gemini enum."""
    if not value:
        return None
    upper = str(value).strip().upper().replace("-", "_")
    if upper == "EXTRA_LOW":
        return "LOW"
    return upper if upper in {"LOW", "MEDIUM", "HIGH"} else None


def _derive_thinking_level(model: str) -> Optional[str]:
    """Thinking level from the catalog's suffixed model id (gemini-4-argon-high)."""
    match = re.search(r"-(extra-low|low|medium|high)$", model)
    if not match:
        return None
    return _normalize_thinking_level(match.group(1))


def _build_request_id() -> str:
    return f"agent/{uuid.uuid4()}/{int(time.time() * 1000)}/{uuid.uuid4()}/1"


def _content_blocks(message: BaseMessage) -> list[Any]:
    content = message.content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _part_from_block(block: Any) -> dict | None:
    """One content block → one Gemini part."""
    if isinstance(block, str):
        return {"text": block}
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype in ("text", "input_text", "output_text"):
        return {"text": block.get("text", "")}
    if btype == "image_url":
        url = (block.get("image_url") or {}).get("url", "")
        if not isinstance(url, str) or not url.startswith("data:"):
            logger.warning("[antigravity] Non-data-URL image block dropped (endpoint takes inline data only)")
            return None
        header, _, data = url.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        return {"inlineData": {"mimeType": mime, "data": data}}
    if btype in ("tool_use", "tool_result"):  # anthropic-style blocks from history
        if btype == "tool_use":
            return {"functionCall": {"name": block.get("name", ""), "args": block.get("input", {})}}
        return None  # handled via ToolMessage path
    if btype == "thinking":
        return None  # reasoning is not replayed (fresh thinking per turn)
    return None


def _messages_to_gemini(messages: Sequence[BaseMessage]) -> tuple[dict | None, list[dict]]:
    """LangChain messages → (systemInstruction, contents).

    AIMessage tool calls become functionCall parts, ToolMessages become
    functionResponse parts, and thought signatures captured from a previous
    response are echoed back as thoughtSignature parts (the endpoint expects
    them on subsequent turns of an agentic loop).
    """
    system_parts: list[dict] = []
    contents: list[dict] = []

    for message in messages:
        if isinstance(message, SystemMessage):
            for block in _content_blocks(message):
                part = _part_from_block(block)
                if part:
                    system_parts.append(part)
            continue

        if isinstance(message, ToolMessage):
            # Gemini expects one functionResponse per tool call, named after
            # the tool, with the payload as a JSON object.
            result: Any = message.content
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = {"result": result}
            elif isinstance(result, list):
                result = {
                    "result": "\n".join(
                        block.get("text", "")
                        for block in result
                        if isinstance(block, dict)
                    )
                }
            if not isinstance(result, dict):
                result = {"result": result}
            contents.append(
                {
                    "role": "user",  # tool results ride the user turn in Gemini
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name or message.tool_call_id or "tool",
                                "response": result,
                            }
                        }
                    ],
                }
            )
            continue

        role = "model" if isinstance(message, AIMessage) else "user"
        parts: list[dict] = []
        for block in _content_blocks(message):
            part = _part_from_block(block)
            if part:
                parts.append(part)
        if isinstance(message, AIMessage):
            # The endpoint validates that functionCall parts carry their
            # thought_signature (it 400s without one); replay captured
            # signatures in order onto the calls, leftovers as standalone.
            signatures = list((message.additional_kwargs or {}).get(_THOUGHT_SIGNATURES_KEY, []))
            for call in message.tool_calls or []:
                part: dict = {
                    "functionCall": {"name": call["name"], "args": call.get("args", {})}
                }
                if signatures:
                    part["thoughtSignature"] = signatures.pop(0)
                parts.append(part)
            for signature in signatures:
                parts.append({"thoughtSignature": signature})
        if parts:
            contents.append({"role": role, "parts": parts})

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


def _openai_tool_to_declaration(tool: dict) -> dict | None:
    """OpenAI-format tool → Gemini functionDeclaration."""
    function = tool.get("function", tool)
    name = function.get("name")
    if not name:
        return None
    declaration: dict[str, Any] = {"name": name}
    if function.get("description"):
        declaration["description"] = function["description"]
    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        # Gemini rejects the OpenAI-only $schema/$defs siblings; keep the
        # JSON-Schema subset it accepts.
        parameters = {
            k: v
            for k, v in parameters.items()
            if k in ("type", "description", "properties", "required", "items", "enum", "format", "anyOf", "title")
        }
        declaration["parameters"] = parameters
    return declaration


class ChatAntigravity(BaseChatModel):
    """Chat model for Google Antigravity subscriptions (OAuth Bearer)."""

    model: str = "gemini-4-argon-medium"
    api_key: str = ""  # OAuth access token (Bearer)
    base_url: str = ANTIGRAVITY_DEFAULT_BASE_URL
    project: str = ANTIGRAVITY_DEFAULT_PROJECT
    user_agent: str = ANTIGRAVITY_USER_AGENT
    max_output_tokens: int = 65536
    thinking_level: Optional[str] = None  # default: derive from model suffix
    include_thoughts: bool = False
    temperature: Optional[float] = None
    timeout: float = 600.0
    default_headers: dict[str, str] = {}
    tools: list[dict] = []  # bound Gemini functionDeclarations

    # Thought signatures observed in the current response, replayed on the
    # next turn of this conversation (per-request, hence private instance).
    _signatures: list[str] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "antigravity"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "project": self.project,
        }

    # -- tool binding ------------------------------------------------------

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        declarations: list[dict] = []
        for tool in tools:
            if isinstance(tool, BaseTool):
                converted = convert_to_openai_tool(tool)
            elif isinstance(tool, dict):
                converted = tool if "function" in tool or "name" in tool else convert_to_openai_tool(tool)
            else:
                converted = convert_to_openai_tool(tool)
            declaration = _openai_tool_to_declaration(converted)
            if declaration:
                declarations.append(declaration)
        return self.model_copy(update={"tools": declarations})

    # -- payload -----------------------------------------------------------

    def _build_payload(
        self,
        messages: Sequence[BaseMessage],
        *,
        stop: Optional[Sequence[str]] = None,
        thinking_level_override: Optional[str] = None,
    ) -> dict:
        system_instruction, contents = _messages_to_gemini(messages)

        inner: dict[str, Any] = {"contents": contents}
        if system_instruction:
            inner["systemInstruction"] = system_instruction
        if stop:
            inner["stopSequences"] = list(stop)

        generation_config: dict[str, Any] = {"maxOutputTokens": self.max_output_tokens}
        if self.temperature is not None:
            generation_config["temperature"] = self.temperature
        level = _normalize_thinking_level(
            thinking_level_override or self.thinking_level
        ) or _derive_thinking_level(self.model)
        if level:
            generation_config["thinkingConfig"] = {
                "includeThoughts": self.include_thoughts,
                "thinkingLevel": level,
            }
        inner["generationConfig"] = generation_config
        if self.tools:
            inner["tools"] = [{"functionDeclarations": self.tools}]
        inner["sessionId"] = str(uuid.uuid4().int & ((1 << 63) - 1))

        return {
            "project": self.project,
            "requestId": _build_request_id(),
            "model": self.model,
            "userAgent": ANTIGRAVITY_BODY_USER_AGENT,
            "requestType": "agent",
            "request": inner,
        }

    def _request_headers(self) -> dict[str, str]:
        return {
            **self.default_headers,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1internal:streamGenerateContent?alt=sse"

    # -- SSE frame handling (shared by sync and async paths) ---------------

    @staticmethod
    def _error_from_status(status: int, body: str) -> AntigravityApiError:
        detail = ""
        try:
            parsed = json.loads(body)
            err = parsed.get("error", {})
            detail = err.get("message", "")
            reason = ""
            for d in err.get("details", []):
                if d.get("@type", "").endswith("ErrorInfo"):
                    reason = d.get("reason", "")
            if reason == "SUBSCRIPTION_REQUIRED":
                return AntigravityApiError(
                    "Antigravity subscription required or no longer valid. "
                    "Reconnect the Google account in Settings.",
                    status, body,
                )
        except json.JSONDecodeError:
            pass
        return AntigravityApiError(
            f"Antigravity API error {status}: {detail or body[:200]}", status, body
        )

    # -- streaming core ----------------------------------------------------

    async def _astream_raw(
        self, payload: dict
    ) -> AsyncIterator[dict]:
        """Yield parsed SSE envelopes from the stream."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", self._endpoint(), json=payload, headers=self._request_headers()
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._error_from_status(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("[antigravity] Dropping malformed SSE frame")

    def _stream_raw(self, payload: dict) -> Iterator[dict]:
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST", self._endpoint(), json=payload, headers=self._request_headers()
            ) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")
                    raise self._error_from_status(response.status_code, body)
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("[antigravity] Dropping malformed SSE frame")

    # -- chunk assembly ----------------------------------------------------

    def _chunks_from_frame(self, envelope: dict) -> list[AIMessageChunk]:
        """One SSE envelope → zero or more AIMessageChunks.

        Thought parts become ``reasoning_content`` in additional_kwargs
        (DeepSeek-style, what the reasoning lineage reads); functionCall
        parts become tool_call_chunks; thoughtSignature parts are captured
        for replay on the next turn.
        """
        response = envelope.get("response") or {}
        candidates = response.get("candidates") or []
        if not candidates:
            error = response.get("error") or envelope.get("error")
            if error:
                raise AntigravityApiError(
                    f"Antigravity stream error: {json.dumps(error)[:300]}"
                )
            return []
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        signatures: list[str] = []
        tool_chunks: list[dict] = []
        for index, part in enumerate(parts):
            if "thoughtSignature" in part:
                signatures.append(part["thoughtSignature"])
            if part.get("thought"):
                if "text" in part:
                    reasoning_parts.append(part["text"])
            elif "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                tool_chunks.append(
                    {
                        "name": call.get("name", ""),
                        "args": json.dumps(call.get("args", {})),
                        "id": f"antigravity_call_{index}",
                        "index": len(tool_chunks),
                    }
                )

        chunks: list[AIMessageChunk] = []
        extra_kwargs: dict[str, Any] = {}
        if signatures:
            self._signatures.extend(signatures)
        if reasoning_parts:
            extra_kwargs["reasoning_content"] = "".join(reasoning_parts)
            chunks.append(AIMessageChunk(content="", additional_kwargs=dict(extra_kwargs)))
        if text_parts:
            content = "".join(text_parts)
            chunk_kwargs = dict(extra_kwargs) if extra_kwargs else {}
            if tool_chunks:
                chunk_kwargs["tool_call_chunks"] = tool_chunks
            chunks.append(AIMessageChunk(content=content, additional_kwargs=chunk_kwargs))
        elif tool_chunks:
            chunks.append(
                AIMessageChunk(content="", additional_kwargs={"tool_call_chunks": tool_chunks})
            )

        finish = candidate.get("finishReason")
        if finish:
            meta: dict[str, Any] = {"finish_reason": finish}
            model_version = response.get("modelVersion")
            if model_version:
                meta["model_version"] = model_version
            response_id = response.get("responseId")
            if response_id:
                meta["response_id"] = response_id
            trace_id = envelope.get("traceId")
            if trace_id:
                meta["trace_id"] = trace_id
            usage = ChatAntigravity._usage_metadata(response)
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={},
                    response_metadata=meta,
                    **({"usage_metadata": usage} if usage else {}),
                )
            )
        return chunks

    @staticmethod
    def _usage_metadata(response: dict) -> UsageMetadata | None:
        usage = response.get("usageMetadata")
        if not usage:
            return None
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        thoughts = usage.get("thoughtsTokenCount") or 0
        details = {"output_token_details": OutputTokenDetails(reasoning_tokens=thoughts)} if thoughts else {}
        return UsageMetadata(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=usage.get("totalTokenCount", input_tokens + output_tokens),
            **details,
        )

    def _final_message(self, chunks: list[AIMessageChunk]) -> AIMessage:
        """Merge streamed chunks into the aggregated AIMessage."""
        text = "".join(c.content for c in chunks if isinstance(c.content, str))
        reasoning = "".join(
            c.additional_kwargs.get("reasoning_content", "")
            for c in chunks
            if c.additional_kwargs.get("reasoning_content")
        )
        tool_calls = []
        for c in chunks:
            for tcc in c.additional_kwargs.get("tool_call_chunks", []) or []:
                args = tcc.get("args") or ""
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {"raw": args}
                tool_calls.append(
                    {
                        "name": tcc.get("name", ""),
                        "args": args,
                        "id": tcc.get("id") or f"antigravity_call_{len(tool_calls)}",
                        "type": "tool_call",
                    }
                )
        additional_kwargs: dict[str, Any] = {}
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        if self._signatures:
            additional_kwargs[_THOUGHT_SIGNATURES_KEY] = list(self._signatures)

        usage = None
        metadata: dict[str, Any] = {}
        for c in reversed(chunks):
            if c.usage_metadata and not usage:
                usage = c.usage_metadata
            if c.response_metadata and not metadata:
                metadata = dict(c.response_metadata)

        return AIMessage(
            content=text,
            tool_calls=tool_calls,
            additional_kwargs=additional_kwargs,
            usage_metadata=usage,
            response_metadata=metadata,
        )

    # -- BaseChatModel API ---------------------------------------------------

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        payload = self._build_payload(messages, stop=stop, thinking_level_override=kwargs.get("thinking_level"))
        for envelope in self._stream_raw(payload):
            for chunk in self._chunks_from_frame(envelope):
                if run_manager and chunk.content:
                    run_manager.on_llm_new_token(chunk.content, chunk=chunk)
                yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        payload = self._build_payload(messages, stop=stop, thinking_level_override=kwargs.get("thinking_level"))
        async for envelope in self._astream_raw(payload):
            for chunk in self._chunks_from_frame(envelope):
                if run_manager and chunk.content:
                    await run_manager.on_llm_new_token(chunk.content, chunk=chunk)
                yield ChatGenerationChunk(message=chunk)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        chunks: list[AIMessageChunk] = []
        for generation in self._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            chunks.append(generation.message)
        message = self._final_message(chunks)
        usage = message.usage_metadata
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={
                "usage": (
                    {
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                    }
                    if usage
                    else {}
                ),
            },
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        chunks: list[AIMessageChunk] = []
        async for generation in self._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            chunks.append(generation.message)
        message = self._final_message(chunks)
        return ChatResult(generations=[ChatGeneration(message=message)])
