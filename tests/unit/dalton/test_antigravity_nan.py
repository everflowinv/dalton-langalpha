"""Antigravity payload serialization rejects no ToolMessage with NaN or Infinity."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.dalton.llm.providers.antigravity.client import ChatAntigravity


def test_build_payload_replaces_nan_and_infinity_with_none() -> None:
    client = ChatAntigravity(
        model="gemini-4-neon-high",
        api_key="fake",
        temperature=float("nan"),
    )
    messages = [
        HumanMessage(content="fetch metrics"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "get_metrics", "args": {"ticker": "FUTU", "limit": float("inf")}, "id": "call_1"}
            ],
        ),
        ToolMessage(
            content='{"pe": NaN, "pb": Infinity, "neg": -Infinity, "series": [1.5, NaN, {"x": Infinity, "y": -Infinity}]}',
            name="get_metrics",
            tool_call_id="call_1",
        ),
    ]

    payload = client._build_payload(messages)
    serialized = json.dumps(payload, allow_nan=False)
    decoded = json.loads(serialized)

    assert payload["request"]["generationConfig"]["temperature"] is None

    tool_resp = decoded["request"]["contents"][2]["parts"][0]["functionResponse"]["response"]
    assert tool_resp == {
        "pe": None,
        "pb": None,
        "neg": None,
        "series": [1.5, None, {"x": None, "y": None}],
    }
    fn_call_args = decoded["request"]["contents"][1]["parts"][0]["functionCall"]["args"]
    assert fn_call_args == {"ticker": "FUTU", "limit": None}
