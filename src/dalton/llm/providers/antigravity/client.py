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
        "model":   "<model_id>",                # TOP-LEVEL, not inside request
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
# Which functionCall each signature rode on, by call id. Its presence marks a
# message whose signatures can be replayed exactly; one without it predates the
# map, and its list cannot be matched to calls.
_CALL_SIGNATURES_KEY = "antigravity_call_signatures"
# Gemini's documented stand-in for a call it did not sign (a Claude call, a
# call from before the map): it validates every functionCall of the current
# turn and refuses one with no signature or with another call's.
_SKIP_SIGNATURE = "skip_thought_signature_validator"


class _SignatureState:
    """The signatures of one response; never shared between calls."""

    def __init__(self) -> None:
        self.ordered: list[str] = []
        self.by_call: dict[str, str] = {}

    def kwargs(self) -> dict[str, Any]:
        if not self.ordered:
            return {}
        return {_THOUGHT_SIGNATURES_KEY: list(self.ordered), _CALL_SIGNATURES_KEY: dict(self.by_call)}


class AntigravityApiError(RuntimeError):
    """Upstream Antigravity call failed. ``status`` and ``status_code`` carry the HTTP code."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.status_code = status
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
        return {"text": block} if block else None
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype in ("text", "input_text", "output_text"):
        text_val = block.get("text", "")
        return {"text": text_val} if text_val else None
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
            fc: dict[str, Any] = {"name": block.get("name", ""), "args": block.get("input", {})}
            if block.get("id"):
                fc["id"] = block["id"]
            return {"functionCall": fc}
        return None  # handled via ToolMessage path
    if btype == "thinking":
        return None  # reasoning is not replayed (fresh thinking per turn)
    return None


def _messages_to_gemini(
    messages: Sequence[BaseMessage], *, gemini_target: bool = False
) -> tuple[dict | None, list[dict]]:
    """LangChain messages → (systemInstruction, contents).

    AIMessage tool calls become functionCall parts, ToolMessages become
    functionResponse parts, and thought signatures captured from a previous
    response are echoed back as thoughtSignature parts (the endpoint expects
    them on subsequent turns of an agentic loop).
    """
    system_parts: list[dict] = []
    contents: list[dict] = []
    # R6-3: map tool_call_id -> tool name from preceding AIMessages so
    # ToolMessages without an explicit .name still send the declared function name.
    tool_call_names: dict[str, str] = {}

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
            resolved_name = (
                message.name
                or tool_call_names.get(message.tool_call_id or "")
                or message.tool_call_id
                or "tool"
            )
            fn_resp: dict[str, Any] = {
                "name": resolved_name,
                "response": result,
            }
            if message.tool_call_id:
                fn_resp["id"] = message.tool_call_id
            contents.append(
                {
                    "role": "user",  # tool results ride the user turn in Gemini
                    "parts": [
                        {
                            "functionResponse": fn_resp
                        }
                    ],
                }
            )
            continue

        role = "model" if isinstance(message, AIMessage) else "user"
        parts: list[dict] = []
        for block in _content_blocks(message):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id") and block.get("name"):
                    tool_call_names[str(block["id"])] = str(block["name"])
            part = _part_from_block(block)
            if part:
                parts.append(part)
        if isinstance(message, AIMessage):
            # The endpoint validates that functionCall parts carry their
            # thought_signature (it 400s without one). A mapped message
            # replays each call's own signature; Gemini gets the stand-in for
            # a call nothing signed, and for every call of an unmapped one,
            # whose list may hold a text part's signature first.
            extra = message.additional_kwargs or {}
            by_call = extra.get(_CALL_SIGNATURES_KEY)
            signatures = list(extra.get(_THOUGHT_SIGNATURES_KEY, []))
            if isinstance(by_call, dict):
                signatures = [sig for sig in signatures if sig not in by_call.values()]
            elif gemini_target:
                signatures = []
            for call in message.tool_calls or []:
                call_id = call.get("id")
                call_name = call.get("name", "")
                if call_id and call_name:
                    tool_call_names[str(call_id)] = str(call_name)
                fc_payload: dict[str, Any] = {"name": call_name, "args": call.get("args", {})}
                if call_id:
                    fc_payload["id"] = call_id
                part_fc: dict = {
                    "functionCall": fc_payload
                }
                if isinstance(by_call, dict):
                    signature = by_call.get(str(call_id)) if call_id else None
                    if signature:
                        part_fc["thoughtSignature"] = signature
                    elif gemini_target:
                        part_fc["thoughtSignature"] = _SKIP_SIGNATURE
                elif gemini_target:
                    part_fc["thoughtSignature"] = _SKIP_SIGNATURE
                elif signatures:
                    part_fc["thoughtSignature"] = signatures.pop(0)
                parts.append(part_fc)
            for signature in signatures:
                parts.append({"thoughtSignature": signature})
        if parts:
            contents.append({"role": role, "parts": parts})

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


_GEMINI_ALLOWED_SCHEMA_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "items",
    "enum",
    "format",
    "anyOf",
    "nullable",
    "title",
}


def _sanitize_gemini_schema(
    schema: Any,
    *,
    is_property: bool = False,
    defs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recursively sanitize a JSON Schema node into the subset Gemini accepts."""
    if not isinstance(schema, dict):
        return {"type": "string"} if is_property else {"type": "object"}

    if defs is None:
        raw_defs = {}
        if isinstance(schema.get("$defs"), dict):
            raw_defs.update(schema["$defs"])
        if isinstance(schema.get("definitions"), dict):
            raw_defs.update(schema["definitions"])
        defs = raw_defs

    node = dict(schema)
    ref_str = node.get("$ref")
    if isinstance(ref_str, str) and defs:
        ref_key = ref_str.rsplit("/", 1)[-1]
        ref_target = defs.get(ref_key)
        if isinstance(ref_target, dict):
            merged_ref = dict(ref_target)
            for k_override, v_override in node.items():
                if k_override != "$ref":
                    merged_ref[k_override] = v_override
            node = merged_ref

    if "allOf" in node and isinstance(node["allOf"], list) and node["allOf"]:
        first = node["allOf"][0]
        if isinstance(first, dict):
            merged = dict(first)
            if "description" in node and "description" not in merged:
                merged["description"] = node["description"]
            node = merged
            if "$ref" in node and isinstance(node["$ref"], str) and defs:
                ref_key = node["$ref"].rsplit("/", 1)[-1]
                ref_target = defs.get(ref_key)
                if isinstance(ref_target, dict):
                    merged_ref = dict(ref_target)
                    for k_override, v_override in node.items():
                        if k_override != "$ref":
                            merged_ref[k_override] = v_override
                    node = merged_ref

    out: dict[str, Any] = {}
    nullable = bool(node.get("nullable", False))

    if "const" in node:
        const_val = node["const"]
        if const_val is None:
            nullable = True
        else:
            out["enum"] = [str(const_val)]
            out["type"] = "string"

    raw_any_of = node.get("anyOf") or node.get("oneOf")
    if isinstance(raw_any_of, list) and raw_any_of:
        non_null_branches: list[dict[str, Any]] = []
        for branch in raw_any_of:
            if isinstance(branch, dict) and branch.get("type") == "null":
                nullable = True
                continue
            cleaned_branch = _sanitize_gemini_schema(branch, is_property=True, defs=defs)
            if cleaned_branch:
                non_null_branches.append(cleaned_branch)
        if non_null_branches:
            single = dict(non_null_branches[0])
            if "description" in node and "description" not in single:
                single["description"] = node["description"]
            out.update(single)

    for k, v in node.items():
        if k in ("anyOf", "oneOf", "allOf", "const"):
            continue
        if k not in _GEMINI_ALLOWED_SCHEMA_KEYS:
            continue
        if k == "type":
            if "anyOf" in out:
                continue
            if isinstance(v, list):
                non_null_types = [t for t in v if t != "null"]
                if "null" in v:
                    nullable = True
                out["type"] = str(non_null_types[0]) if non_null_types else "string"
            elif isinstance(v, str):
                if v == "null":
                    nullable = True
                    out["type"] = "string"
                else:
                    out["type"] = v
        elif k == "properties" and isinstance(v, dict):
            props = {
                str(prop_name): _sanitize_gemini_schema(prop_val, is_property=True, defs=defs)
                for prop_name, prop_val in v.items()
            }
            out["properties"] = props
        elif k == "items":
            if isinstance(v, dict):
                out["items"] = _sanitize_gemini_schema(v, is_property=True, defs=defs)
            elif isinstance(v, list) and v:
                out["items"] = _sanitize_gemini_schema(v[0], is_property=True, defs=defs)
        elif k == "enum" and isinstance(v, list):
            non_null_enums = [str(item) for item in v if item is not None]
            if any(item is None for item in v):
                nullable = True
            if non_null_enums:
                out["enum"] = non_null_enums
                out.setdefault("type", "string")
        elif k == "required" and isinstance(v, list):
            out["required"] = [str(r) for r in v if isinstance(r, str)]
        else:
            out[k] = v

    if "required" in out and "properties" in out:
        valid_props = set(out["properties"].keys())
        out["required"] = [r for r in out["required"] if r in valid_props]
        if not out["required"]:
            out.pop("required", None)
    elif "required" in out and "properties" not in out:
        out.pop("required", None)

    if nullable:
        out["nullable"] = True

    if "type" not in out and "anyOf" not in out:
        out["type"] = "object" if "properties" in out or not is_property else "string"

    return out


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
        try:
            from langchain_core.utils.json_schema import dereference_refs

            deref = dereference_refs(parameters)
            if isinstance(deref, dict):
                parameters = deref
        except Exception as exc:
            logger.debug("[antigravity] dereference_refs failed for tool %s: %s", name, exc)
        declaration["parameters"] = _sanitize_gemini_schema(parameters, is_property=False)
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
        system_instruction, contents = _messages_to_gemini(
            messages, gemini_target="gemini" in (self.model or "").lower()
        )

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
        effective_status = status or 500
        try:
            parsed = json.loads(body)
            err = parsed.get("error", {})
            detail = err.get("message", "")
            reason = ""
            for d in err.get("details", []):
                if d.get("@type", "").endswith("ErrorInfo"):
                    reason = d.get("reason", "")
            if reason == "SUBSCRIPTION_REQUIRED":
                sub_status = status or 403
                return AntigravityApiError(
                    f"Antigravity API error {sub_status}: Antigravity subscription required or no longer valid. "
                    "Reconnect the Google account in Settings.",
                    sub_status,
                    body,
                )
        except json.JSONDecodeError:
            pass
        return AntigravityApiError(
            f"Antigravity API error {effective_status}: {detail or body[:200]}",
            effective_status,
            body,
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

    def _chunks_from_frame(
        self, envelope: dict, sigs: _SignatureState | None = None
    ) -> list[AIMessageChunk]:
        """One SSE envelope → zero or more AIMessageChunks.

        Thought parts become ``reasoning_content`` in additional_kwargs
        (DeepSeek-style, what the reasoning lineage reads); functionCall
        parts become tool_call_chunks; thoughtSignature parts are captured
        for replay on the next turn.
        """
        # Per call, not on the client: one cached client serves concurrent
        # turns, and a shared list mixed their signatures.
        sigs = sigs if sigs is not None else _SignatureState()
        response = envelope.get("response") or {}
        candidates = response.get("candidates") or []
        if not candidates:
            error = response.get("error") or envelope.get("error")
            if error:
                err_code = error.get("code") if isinstance(error, dict) else None
                status = int(err_code) if isinstance(err_code, int) and 100 <= err_code < 600 else 500
                raise AntigravityApiError(
                    f"Antigravity stream error {status}: {json.dumps(error)[:300]}",
                    status=status,
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
                call_id = call.get("id") or f"antigravity_call_{uuid.uuid4().hex[:8]}"
                if "thoughtSignature" in part:
                    sigs.by_call[call_id] = part["thoughtSignature"]
                tool_chunks.append(
                    {
                        "name": call.get("name", ""),
                        "args": json.dumps(call.get("args", {})),
                        "id": call_id,
                        "index": len(tool_chunks),
                        "type": "tool_call_chunk",
                    }
                )

        chunks: list[AIMessageChunk] = []
        sigs.ordered.extend(signatures)
        if reasoning_parts:
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "".join(reasoning_parts)},
                )
            )
        # R6-4: do NOT copy reasoning_content onto the text chunk when a single
        # SSE frame contains both thought and text parts.
        if text_parts:
            content = "".join(text_parts)
            chunk_kwargs: dict[str, Any] = {}
            if tool_chunks:
                chunk_kwargs["tool_call_chunks"] = tool_chunks
            chunks.append(
                AIMessageChunk(
                    content=content,
                    tool_call_chunks=tool_chunks if tool_chunks else [],
                    additional_kwargs=chunk_kwargs,
                )
            )
        elif tool_chunks:
            chunks.append(
                AIMessageChunk(
                    content="",
                    tool_call_chunks=tool_chunks,
                    additional_kwargs={"tool_call_chunks": tool_chunks},
                )
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
            finish_kwargs: dict[str, Any] = sigs.kwargs()
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs=finish_kwargs,
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
        candidates_tokens = usage.get("candidatesTokenCount", 0)
        thoughts = usage.get("thoughtsTokenCount") or 0
        # R6-13: include thoughts in output_tokens so input_tokens + output_tokens == total_tokens
        output_tokens = candidates_tokens + thoughts
        details = {"output_token_details": OutputTokenDetails(reasoning=thoughts)} if thoughts else {}
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
        for c in chunks:
            if c.additional_kwargs.get(_THOUGHT_SIGNATURES_KEY):
                additional_kwargs[_THOUGHT_SIGNATURES_KEY] = list(c.additional_kwargs[_THOUGHT_SIGNATURES_KEY])
                additional_kwargs[_CALL_SIGNATURES_KEY] = dict(c.additional_kwargs.get(_CALL_SIGNATURES_KEY) or {})

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
        sigs = _SignatureState()
        payload = self._build_payload(messages, stop=stop, thinking_level_override=kwargs.get("thinking_level"))
        for envelope in self._stream_raw(payload):
            for chunk in self._chunks_from_frame(envelope, sigs):
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
        sigs = _SignatureState()
        payload = self._build_payload(messages, stop=stop, thinking_level_override=kwargs.get("thinking_level"))
        async for envelope in self._astream_raw(payload):
            for chunk in self._chunks_from_frame(envelope, sigs):
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
