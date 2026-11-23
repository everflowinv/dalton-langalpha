#!/usr/bin/env python3
"""Fake OpenAI Responses API server for standalone core CI verification.

Implements the exact wire contract used by ``src/llms/llm.py`` when a custom
provider declares ``parent_provider="openai"`` and ``use_response_api=True``
(``ChatOpenAI(..., output_version="responses/v1")``):

- ``GET /health``: container readiness probe
- ``GET /v1/models``: model listing (used by key probes and discovery)
- ``POST /v1/responses``: OpenAI Responses API (streaming SSE & non-streaming)

Behavior is deterministic and driven by the latest user turn / tool output:
1. Normal greeting -> streams fixed greeting text + token usage.
2. ``RUN_CODE`` (or ``RUN_CODE_B64:<base64>``) -> round 1 emits a valid
   ``ExecuteCode`` ``function_call`` (matching
   ``src/ptc_agent/agent/tools/code_execution.py``); round 2 reads the
   ``function_call_output`` item and echoes the sandbox output verbatim.
3. ``SLOW_STREAM`` -> streams 10 numbered text chunks with a small delay so
   mid-stream disconnect + ``last_event_id`` reconnection is deterministic.
4. ``REPORT_TOOLS`` -> echoes the sorted bound tool names received in
   ``payload["tools"]`` so E2E checks can assert ``WebFetch`` vs ``WebSearch``.

All request handling is factored into pure functions (``handle_models_request``
and ``handle_responses_request``) so unit tests exercise the full protocol
without opening sockets.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import time
from typing import Any
import uuid

DEFAULT_TEST_MODEL = "gpt-6-luna"
DEFAULT_GREETING_TEXT = (
    "Hello! This is the standalone core fake model responding via the "
    "OpenAI Responses API."
)
DEFAULT_CODE_SNIPPET = 'print("FAKE_SANDBOX_OK:6x7=" + str(6 * 7))'
DEFAULT_SLOW_CHUNK_DELAY_S = 0.25

_RUN_CODE_B64_RE = re.compile(r"RUN_CODE_B64:([A-Za-z0-9+/=_-]+)")


@dataclass(frozen=True)
class FakeResponseResult:
    """Pure return value produced by :func:`handle_responses_request`."""

    status: int
    mode: str
    is_stream: bool = False
    json_body: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    chunk_delay_s: float = 0.0

    def sse_bytes(self) -> bytes:
        return b"".join(format_sse_event(ev) for ev in self.events)


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def validate_authorization(headers: Mapping[str, str]) -> dict[str, Any] | None:
    """Return an OpenAI-shaped error payload if ``Authorization`` is invalid."""
    raw = _get_header(headers, "authorization")
    if not raw or not isinstance(raw, str):
        return {
            "error": {
                "message": "Missing Authorization header.",
                "type": "authentication_error",
                "param": None,
                "code": "missing_api_key",
            }
        }
    parts = raw.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return {
            "error": {
                "message": "Invalid Authorization header; expected 'Bearer <token>'.",
                "type": "authentication_error",
                "param": None,
                "code": "invalid_api_key",
            }
        }
    return None


def _normalize_input_items(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]
    if isinstance(raw_input, list):
        return [item for item in raw_input if isinstance(item, dict)]
    return []


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _extract_tool_output_text(item: dict[str, Any]) -> str:
    if item.get("type") == "function_call_output":
        out = item.get("output")
        if isinstance(out, str):
            return out
        return _extract_text_from_content(out)
    if item.get("role") == "tool":
        return _extract_text_from_content(item.get("content"))
    return ""


def _last_user_index_and_text(items: list[dict[str, Any]]) -> tuple[int, str]:
    last_idx = -1
    last_text = ""
    for idx, item in enumerate(items):
        if item.get("role") == "user":
            last_idx = idx
            last_text = _extract_text_from_content(item.get("content"))
    return last_idx, last_text


def _trailing_tool_outputs(items: list[dict[str, Any]], after_index: int) -> list[str]:
    outputs: list[str] = []
    start = after_index + 1 if after_index >= 0 else 0
    for item in items[start:]:
        if item.get("type") == "function_call_output" or item.get("role") == "tool":
            outputs.append(_extract_tool_output_text(item))
    return outputs


def _extract_tool_names(raw_tools: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(raw_tools, list):
        return names
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(set(names))


def _resolve_code_snippet(user_text: str) -> str:
    match = _RUN_CODE_B64_RE.search(user_text)
    if match:
        token = match.group(1)
        padded = token + "=" * (-len(token) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            if decoded.strip():
                return decoded
        except Exception:
            pass
    return DEFAULT_CODE_SNIPPET


def _estimate_tokens(text: str, minimum: int = 16) -> int:
    return max(minimum, len(text.encode("utf-8")) // 4 + 1)


def _build_usage(input_text: str, output_text: str) -> dict[str, Any]:
    input_tokens = _estimate_tokens(input_text, minimum=32)
    output_tokens = _estimate_tokens(output_text, minimum=12)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _base_response_object(
    *,
    response_id: str,
    created_at: int,
    model: str,
    status: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
        "metadata": {},
    }


def _split_chunks(text: str, chunk_size: int = 24) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _build_text_events(
    *,
    response_id: str,
    created_at: int,
    model: str,
    chunks: list[str],
    usage: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    full_text = "".join(chunks)
    in_progress_resp = _base_response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="in_progress",
        output=[],
        usage=None,
    )
    final_item = {
        "id": msg_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": full_text,
                "annotations": [],
            }
        ],
    }
    completed_resp = _base_response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="completed",
        output=[final_item],
        usage=usage,
    )

    seq = 0

    def _ev(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        payload["sequence_number"] = seq
        seq += 1
        return payload

    events: list[dict[str, Any]] = [
        _ev({"type": "response.created", "response": in_progress_resp}),
        _ev({"type": "response.in_progress", "response": in_progress_resp}),
        _ev(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": msg_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }
        ),
        _ev(
            {
                "type": "response.content_part.added",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }
        ),
    ]
    for delta in chunks:
        events.append(
            _ev(
                {
                    "type": "response.output_text.delta",
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": delta,
                    "logprobs": [],
                }
            )
        )
    events.extend(
        [
            _ev(
                {
                    "type": "response.output_text.done",
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": full_text,
                    "logprobs": [],
                }
            ),
            _ev(
                {
                    "type": "response.content_part.done",
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": full_text,
                        "annotations": [],
                    },
                }
            ),
            _ev(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": final_item,
                }
            ),
            _ev({"type": "response.completed", "response": completed_resp}),
        ]
    )
    return events, completed_resp


def _build_function_call_events(
    *,
    response_id: str,
    created_at: int,
    model: str,
    tool_name: str,
    arguments_obj: dict[str, Any],
    usage: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fc_id = f"fc_{uuid.uuid4().hex[:24]}"
    call_id = f"call_{uuid.uuid4().hex[:24]}"
    args_json = json.dumps(arguments_obj, ensure_ascii=False)
    arg_chunks = _split_chunks(args_json, chunk_size=28)

    in_progress_resp = _base_response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="in_progress",
        output=[],
        usage=None,
    )
    final_item = {
        "id": fc_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": tool_name,
        "arguments": args_json,
    }
    completed_resp = _base_response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="completed",
        output=[final_item],
        usage=usage,
    )

    seq = 0

    def _ev(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        payload["sequence_number"] = seq
        seq += 1
        return payload

    events: list[dict[str, Any]] = [
        _ev({"type": "response.created", "response": in_progress_resp}),
        _ev({"type": "response.in_progress", "response": in_progress_resp}),
        _ev(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": fc_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": "",
                },
            }
        ),
    ]
    for delta in arg_chunks:
        events.append(
            _ev(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": fc_id,
                    "output_index": 0,
                    "delta": delta,
                }
            )
        )
    events.extend(
        [
            _ev(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": fc_id,
                    "output_index": 0,
                    "name": tool_name,
                    "arguments": args_json,
                }
            ),
            _ev(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": final_item,
                }
            ),
            _ev({"type": "response.completed", "response": completed_resp}),
        ]
    )
    return events, completed_resp


def format_sse_event(event: Mapping[str, Any]) -> bytes:
    """Format a single Responses API event dict as an SSE wire frame."""
    event_type = str(event.get("type") or "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def handle_models_request(
    headers: Mapping[str, str],
    *,
    expected_model: str = DEFAULT_TEST_MODEL,
) -> tuple[int, dict[str, Any]]:
    """Pure handler for ``GET /v1/models``."""
    auth_error = validate_authorization(headers)
    if auth_error is not None:
        return 401, auth_error
    return 200, {
        "object": "list",
        "data": [
            {
                "id": expected_model,
                "object": "model",
                "created": 1700000000,
                "owned_by": "fake-openai",
            }
        ],
    }


def handle_responses_request(
    headers: Mapping[str, str],
    body: Any,
    *,
    expected_model: str = DEFAULT_TEST_MODEL,
    slow_chunk_delay_s: float = DEFAULT_SLOW_CHUNK_DELAY_S,
) -> FakeResponseResult:
    """Pure handler for ``POST /v1/responses``."""
    auth_error = validate_authorization(headers)
    if auth_error is not None:
        return FakeResponseResult(status=401, mode="error", json_body=auth_error)

    if not isinstance(body, dict):
        return FakeResponseResult(
            status=400,
            mode="error",
            json_body={
                "error": {
                    "message": "Request body must be a JSON object.",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_body",
                }
            },
        )

    model = body.get("model")
    if not isinstance(model, str) or model != expected_model:
        return FakeResponseResult(
            status=400,
            mode="error",
            json_body={
                "error": {
                    "message": (
                        f"Unsupported model {model!r}; expected {expected_model!r}."
                    ),
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }
            },
        )

    items = _normalize_input_items(body.get("input"))
    last_user_idx, user_text = _last_user_index_and_text(items)
    trailing_outputs = _trailing_tool_outputs(items, last_user_idx)
    serialized_input = json.dumps(body.get("input", ""), ensure_ascii=False)

    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())
    stream = bool(body.get("stream", False))

    if trailing_outputs:
        combined_tool_output = "\n".join(trailing_outputs)
        final_text = f"Tool execution completed.\n{combined_tool_output}"
        usage = _build_usage(serialized_input, final_text)
        events, completed = _build_text_events(
            response_id=response_id,
            created_at=created_at,
            model=model,
            chunks=_split_chunks(final_text, chunk_size=32),
            usage=usage,
        )
        return FakeResponseResult(
            status=200,
            mode="tool_result",
            is_stream=stream,
            json_body=None if stream else completed,
            events=events,
        )

    if "REPORT_TOOLS" in user_text:
        tool_names = _extract_tool_names(body.get("tools"))
        final_text = f"BOUND_TOOLS={json.dumps(tool_names, ensure_ascii=False)}"
        usage = _build_usage(serialized_input, final_text)
        events, completed = _build_text_events(
            response_id=response_id,
            created_at=created_at,
            model=model,
            chunks=_split_chunks(final_text, chunk_size=32),
            usage=usage,
        )
        return FakeResponseResult(
            status=200,
            mode="report_tools",
            is_stream=stream,
            json_body=None if stream else completed,
            events=events,
        )

    if "RUN_CODE" in user_text:
        code = _resolve_code_snippet(user_text)
        arguments_obj = {
            "code": code,
            "description": "Execute verification Python code in sandbox",
        }
        args_json = json.dumps(arguments_obj, ensure_ascii=False)
        usage = _build_usage(serialized_input, args_json)
        events, completed = _build_function_call_events(
            response_id=response_id,
            created_at=created_at,
            model=model,
            tool_name="ExecuteCode",
            arguments_obj=arguments_obj,
            usage=usage,
        )
        return FakeResponseResult(
            status=200,
            mode="tool_call",
            is_stream=stream,
            json_body=None if stream else completed,
            events=events,
        )

    if "SLOW_STREAM" in user_text:
        chunks = [f"Segment {i:02d}: streaming progress. " for i in range(1, 10)]
        chunks.append("Segment 10: SLOW_STREAM_COMPLETE.")
        full_text = "".join(chunks)
        usage = _build_usage(serialized_input, full_text)
        events, completed = _build_text_events(
            response_id=response_id,
            created_at=created_at,
            model=model,
            chunks=chunks,
            usage=usage,
        )
        return FakeResponseResult(
            status=200,
            mode="slow_greeting",
            is_stream=stream,
            json_body=None if stream else completed,
            events=events,
            chunk_delay_s=max(0.0, float(slow_chunk_delay_s)),
        )

    usage = _build_usage(serialized_input, DEFAULT_GREETING_TEXT)
    events, completed = _build_text_events(
        response_id=response_id,
        created_at=created_at,
        model=model,
        chunks=_split_chunks(DEFAULT_GREETING_TEXT, chunk_size=24),
        usage=usage,
    )
    return FakeResponseResult(
        status=200,
        mode="greeting",
        is_stream=stream,
        json_body=None if stream else completed,
        events=events,
    )


def _make_handler(
    *,
    expected_model: str,
    slow_chunk_delay_s: float,
) -> type[BaseHTTPRequestHandler]:
    class FakeOpenAIHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(200, {"status": "ok", "model": expected_model})
                return
            if path in ("/v1/models", "/models"):
                status, payload = handle_models_request(
                    dict(self.headers.items()),
                    expected_model=expected_model,
                )
                self._send_json(status, payload)
                return
            self._send_json(
                404,
                {
                    "error": {
                        "message": f"Not found: {path}",
                        "type": "invalid_request_error",
                        "code": "not_found",
                    }
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/v1/responses", "/responses"):
                self._send_json(
                    404,
                    {
                        "error": {
                            "message": f"Not found: {path}",
                            "type": "invalid_request_error",
                            "code": "not_found",
                        }
                    },
                )
                return

            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    400,
                    {
                        "error": {
                            "message": "Invalid JSON body.",
                            "type": "invalid_request_error",
                            "code": "invalid_json",
                        }
                    },
                )
                return

            result = handle_responses_request(
                dict(self.headers.items()),
                body,
                expected_model=expected_model,
                slow_chunk_delay_s=slow_chunk_delay_s,
            )
            if not result.is_stream or result.status != 200:
                self._send_json(result.status, result.json_body or {})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for ev in result.events:
                    frame = format_sse_event(ev)
                    self.wfile.write(frame)
                    self.wfile.flush()
                    if (
                        result.chunk_delay_s > 0
                        and ev.get("type") == "response.output_text.delta"
                    ):
                        time.sleep(result.chunk_delay_s)
            except BrokenPipeError:
                pass

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"[fake-llm] {self.address_string()} - {format % args}", flush=True)

    return FakeOpenAIHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OpenAI Responses API server")
    parser.add_argument("--host", default=os.getenv("FAKE_LLM_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("FAKE_LLM_PORT", "8099"))
    )
    parser.add_argument(
        "--model", default=os.getenv("FAKE_LLM_MODEL", DEFAULT_TEST_MODEL)
    )
    parser.add_argument(
        "--slow-chunk-delay",
        type=float,
        default=float(
            os.getenv("FAKE_LLM_CHUNK_DELAY_S", str(DEFAULT_SLOW_CHUNK_DELAY_S))
        ),
    )
    args = parser.parse_args()

    handler_cls = _make_handler(
        expected_model=args.model,
        slow_chunk_delay_s=args.slow_chunk_delay,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(
        f"[fake-llm] Listening on http://{args.host}:{args.port} "
        f"(model={args.model})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
