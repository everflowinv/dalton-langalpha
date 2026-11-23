"""Offline unit tests for ``tests/fakes/fake_openai_server.py``.

Calls pure handler functions directly (zero sockets) and additionally verifies
wire-level compatibility with ``langchain_openai.ChatOpenAI`` (Responses API
``output_version="responses/v1"``) via an in-process ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
import pytest

from tests.fakes.fake_openai_server import (
    DEFAULT_GREETING_TEXT,
    DEFAULT_TEST_MODEL,
    handle_models_request,
    handle_responses_request,
)

AUTH_HEADERS = {"Authorization": "Bearer sk-fake-test-token"}


def _parse_sse_frames(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw.decode("utf-8").strip().split("\n\n"):
        event_name = ""
        data_line = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_line = line[len("data:") :].strip()
        assert event_name and data_line, f"Malformed SSE frame: {block!r}"
        frames.append((event_name, json.loads(data_line)))
    return frames


def test_models_endpoint_requires_auth_and_lists_test_model() -> None:
    status, body = handle_models_request({})
    assert status == 401
    assert body["error"]["code"] == "missing_api_key"

    status, body = handle_models_request({"Authorization": "Basic abc"})
    assert status == 401
    assert body["error"]["code"] == "invalid_api_key"

    status, body = handle_models_request(AUTH_HEADERS)
    assert status == 200
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == [DEFAULT_TEST_MODEL]


def test_responses_validates_auth_and_model_name() -> None:
    res = handle_responses_request({}, {"model": DEFAULT_TEST_MODEL, "input": "hi"})
    assert res.status == 401

    res = handle_responses_request(
        AUTH_HEADERS,
        {"model": "unexpected-model", "input": "hi", "stream": True},
    )
    assert res.status == 400
    assert res.json_body is not None
    assert res.json_body["error"]["code"] == "model_not_found"


def test_greeting_streams_fixed_text_and_usage() -> None:
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": True,
            "input": [{"role": "user", "content": "Hello there!"}],
        },
    )
    assert res.status == 200
    assert res.mode == "greeting"
    assert res.is_stream is True

    frames = _parse_sse_frames(res.sse_bytes())
    types = [name for name, _ in frames]
    assert types[0] == "response.created"
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert types[-1] == "response.completed"

    reconstructed = "".join(
        payload["delta"]
        for name, payload in frames
        if name == "response.output_text.delta"
    )
    assert reconstructed == DEFAULT_GREETING_TEXT

    completed = frames[-1][1]["response"]
    usage = completed["usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


def test_run_code_round1_emits_valid_execute_code_function_call() -> None:
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": True,
            "input": [{"role": "user", "content": "RUN_CODE verify sandbox"}],
        },
    )
    assert res.status == 200
    assert res.mode == "tool_call"

    frames = _parse_sse_frames(res.sse_bytes())
    types = [name for name, _ in frames]
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert types[-1] == "response.completed"

    added = next(p for n, p in frames if n == "response.output_item.added")
    assert added["item"]["type"] == "function_call"
    assert added["item"]["name"] == "ExecuteCode"
    assert added["item"]["call_id"].startswith("call_")
    assert added["item"]["id"].startswith("fc_")

    arg_deltas = "".join(
        p["delta"] for n, p in frames if n == "response.function_call_arguments.delta"
    )
    args = json.loads(arg_deltas)
    assert set(args.keys()) == {"code", "description"}
    assert "FAKE_SANDBOX_OK" in args["code"]
    assert isinstance(args["description"], str) and args["description"]

    usage = frames[-1][1]["response"]["usage"]
    assert usage["total_tokens"] > 0


def test_run_code_b64_overrides_snippet() -> None:
    snippet = 'print("CUSTOM_SNIPPET=99")'
    b64 = base64.urlsafe_b64encode(snippet.encode("utf-8")).decode("ascii")
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": False,
            "input": [{"role": "user", "content": f"RUN_CODE RUN_CODE_B64:{b64}"}],
        },
    )
    assert res.status == 200
    assert res.json_body is not None
    fc = res.json_body["output"][0]
    assert fc["type"] == "function_call"
    assert fc["name"] == "ExecuteCode"
    assert json.loads(fc["arguments"])["code"] == snippet


def test_run_code_round2_echoes_tool_output_verbatim_with_usage() -> None:
    sandbox_stdout = "SUCCESS\nFAKE_SANDBOX_OK:6x7=42\nEXTRA_LINE=abc"
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": True,
            "input": [
                {"role": "user", "content": "RUN_CODE verify sandbox"},
                {
                    "type": "function_call",
                    "id": "fc_001",
                    "call_id": "call_001",
                    "name": "ExecuteCode",
                    "arguments": json.dumps({"code": "print(42)"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_001",
                    "output": sandbox_stdout,
                },
            ],
        },
    )
    assert res.status == 200
    assert res.mode == "tool_result"

    frames = _parse_sse_frames(res.sse_bytes())
    text = "".join(
        p["delta"] for n, p in frames if n == "response.output_text.delta"
    )
    assert sandbox_stdout in text
    assert frames[-1][1]["response"]["usage"]["output_tokens"] > 0


def test_report_tools_echoes_bound_tool_names() -> None:
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": True,
            "input": [{"role": "user", "content": "REPORT_TOOLS"}],
            "tools": [
                {"type": "function", "name": "ExecuteCode", "parameters": {}},
                {"type": "function", "name": "WebFetch", "parameters": {}},
            ],
        },
    )
    assert res.status == 200
    assert res.mode == "report_tools"
    frames = _parse_sse_frames(res.sse_bytes())
    text = "".join(p["delta"] for n, p in frames if n == "response.output_text.delta")
    assert 'BOUND_TOOLS=["ExecuteCode", "WebFetch"]' in text


def test_wire_compatibility_with_langchain_chat_openai_responses_api() -> None:
    """Drive real ``ChatOpenAI(output_version='responses/v1')`` over MockTransport."""

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content.decode("utf-8"))
        res = handle_responses_request(
            dict(request.headers.items()),
            payload,
            expected_model=DEFAULT_TEST_MODEL,
            slow_chunk_delay_s=0.0,
        )
        if res.is_stream and res.status == 200:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                content=res.sse_bytes(),
            )
        return httpx.Response(res.status, json=res.json_body)

    http_client = httpx.Client(transport=httpx.MockTransport(_mock_handler))
    llm = ChatOpenAI(
        model=DEFAULT_TEST_MODEL,
        api_key="sk-fake-wire-test",
        base_url="http://fake-llm:8099/v1",
        output_version="responses/v1",
        stream_usage=True,
        http_client=http_client,
    )

    # 1. Greeting stream
    greeting_acc = None
    for chunk in llm.stream([HumanMessage(content="Hello!")]):
        greeting_acc = chunk if greeting_acc is None else greeting_acc + chunk
    assert greeting_acc is not None
    assert DEFAULT_GREETING_TEXT in greeting_acc.text
    assert greeting_acc.usage_metadata is not None
    assert greeting_acc.usage_metadata["input_tokens"] > 0
    assert greeting_acc.usage_metadata["output_tokens"] > 0

    # 2. Tool call round 1 (ExecuteCode)
    tc_acc = None
    for chunk in llm.stream([HumanMessage(content="RUN_CODE test")]):
        tc_acc = chunk if tc_acc is None else tc_acc + chunk
    assert tc_acc is not None
    assert len(tc_acc.tool_calls) == 1
    tc = tc_acc.tool_calls[0]
    assert tc["name"] == "ExecuteCode"
    assert tc["id"].startswith("call_")
    assert "FAKE_SANDBOX_OK" in tc["args"]["code"]
    assert tc_acc.usage_metadata is not None

    # 3. Tool call round 2 (echo sandbox output)
    follow_acc = None
    ai_msg = AIMessage(content="", tool_calls=[tc])
    tool_msg = ToolMessage(
        content="SUCCESS\nFAKE_SANDBOX_OK:6x7=42",
        tool_call_id=tc["id"],
        name="ExecuteCode",
    )
    for chunk in llm.stream([HumanMessage(content="RUN_CODE test"), ai_msg, tool_msg]):
        follow_acc = chunk if follow_acc is None else follow_acc + chunk
    assert follow_acc is not None
    assert "FAKE_SANDBOX_OK:6x7=42" in follow_acc.text
    assert follow_acc.usage_metadata is not None


@pytest.mark.parametrize("stream", [False, True])
def test_slow_stream_mode_reaches_completion_marker(stream: bool) -> None:
    res = handle_responses_request(
        AUTH_HEADERS,
        {
            "model": DEFAULT_TEST_MODEL,
            "stream": stream,
            "input": [{"role": "user", "content": "SLOW_STREAM check"}],
        },
        slow_chunk_delay_s=0.0,
    )
    assert res.status == 200
    assert res.mode == "slow_greeting"
    if stream:
        frames = _parse_sse_frames(res.sse_bytes())
        deltas = [p["delta"] for n, p in frames if n == "response.output_text.delta"]
        assert len(deltas) == 10
        assert "SLOW_STREAM_COMPLETE" in "".join(deltas)
    else:
        assert res.json_body is not None
        text = res.json_body["output"][0]["content"][0]["text"]
        assert "SLOW_STREAM_COMPLETE" in text
