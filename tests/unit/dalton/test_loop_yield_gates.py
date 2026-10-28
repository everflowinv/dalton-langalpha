"""A1(4): the synchronous paths that see untrusted-size inputs yield the loop.

Two gates: compaction's incremental token count (per-call, so only large
additions hop to a thread) and provenance's extraction/emit-prep (bounded
inputs stay inline, fat batches move off the loop).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ptc_agent.agent.middleware.compaction import measure as measure_mod
from ptc_agent.agent.middleware.compaction.measure import (
    _ADDED_CHARS_THREAD_FLOOR,
    _content_chars,
    context_tokens,
)
from ptc_agent.agent.middleware.compaction.types import TokenCounter
from ptc_agent.agent.middleware.provenance.middleware import (
    _LARGE_RESULT_CHARS,
    ProvenanceMiddleware,
    _result_volume,
)
from ptc_agent.agent.provenance.types import ProvenanceSource


def _counter(record_thread: dict[str, int]) -> TokenCounter:
    def _count(messages: Any) -> int:
        record_thread["thread"] = threading.get_ident()
        return sum(len(str(getattr(m, "content", ""))) for m in messages) // 4

    return _count  # type: ignore[return-value]


def test_content_chars_sums_strings_and_blocks() -> None:
    msgs: list[Any] = [
        HumanMessage(content="abc"),
        ToolMessage(content=[{"type": "text", "text": "x" * 10}], tool_call_id="t"),
    ]
    # A string counts as its length; a block list counts each part's string
    # form — a rough size proxy, exact value irrelevant to the gate.
    assert _content_chars(msgs) > 3 + 10
    assert _content_chars([HumanMessage(content="abc")]) == 3


@pytest.mark.asyncio
async def test_small_addition_estimates_on_the_loop() -> None:
    record: dict[str, int] = {}
    view = [
        AIMessage(content="answer"),
        ToolMessage(content="small result", tool_call_id="t"),
    ]
    reading = await context_tokens(
        {"_cached_input_tokens": 100, "_cached_output_tokens": 10},
        view,
        measure_mod.Estimate(_counter(record), None, "model-x"),
    )
    assert reading.answer is view[0]
    assert record["thread"] == threading.get_ident()
    assert reading.tokens > 110  # measured + the small estimate


@pytest.mark.asyncio
async def test_large_addition_estimates_on_a_thread() -> None:
    record: dict[str, int] = {}
    big = ToolMessage(content="x" * (_ADDED_CHARS_THREAD_FLOOR + 1000), tool_call_id="t")
    view = [AIMessage(content="answer"), big]
    await context_tokens(
        {"_cached_input_tokens": 100, "_cached_output_tokens": 10},
        view,
        measure_mod.Estimate(_counter(record), None, "model-x"),
    )
    assert record["thread"] != threading.get_ident()


def test_result_volume_counts_content_and_trace_bodies() -> None:
    class _Result:
        def __init__(self, content: str, artifact: Any) -> None:
            self.content = content
            self.artifact = artifact

    small = _Result("x" * 100, None)
    assert _result_volume(small) == 100

    fat_trace = _Result(
        "",
        {
            "mcp_trace": [
                {"result_body": "b" * (_LARGE_RESULT_CHARS // 2)},
                {"result_body": "b" * (_LARGE_RESULT_CHARS // 2 + 1)},
            ]
        },
    )
    assert _result_volume(fat_trace) >= _LARGE_RESULT_CHARS


@pytest.mark.asyncio
async def test_emit_prep_moves_to_thread_only_for_fat_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: list[int] = []

    def _redactor(text: str | None) -> str | None:
        if text is not None:
            threads.append(threading.get_ident())
        return text

    mw = ProvenanceMiddleware(redactor=_redactor)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.provenance.middleware.get_stream_writer",
        lambda: (lambda event: None),
    )

    def _source(body: str | None, sha: str | None = "0" * 64) -> ProvenanceSource:
        return ProvenanceSource(
            record_id="r",
            source_type="web_fetch",
            identifier="https://example.com",
            timestamp="t",
            result_sha256=sha,
            result_body=body,
        )

    # Small batch: prepared inline, on the loop thread.
    await mw._emit([_source("short body")], "probe")
    assert threads and threads[0] == threading.get_ident()

    threads.clear()
    # Fat batch: summed bodies past the threshold prepare on a worker thread.
    await mw._emit([_source("z" * (_LARGE_RESULT_CHARS + 1))], "probe")
    assert threads and threads[0] != threading.get_ident()
