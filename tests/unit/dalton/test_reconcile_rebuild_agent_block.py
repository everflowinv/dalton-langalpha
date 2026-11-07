"""Knowledge written between turns reaches the model through the turn-start
rebuild (B12, PK.5).

The agent wires ``DaltonReconcileMiddleware`` (only ``abefore_agent``) ahead
of ``BaselineContextMiddleware``, which freezes the epoch reading ``agent.md``
at the same hook, and the compaction middleware — outside both — performs the
turn-start summary in ``awrap_model_call``, after every ``before_agent`` node
has run. So a knowledge version that landed while the thread was idle is
applied before the turn reads anything: the boundary read already sees the new
``agent.md``, and the turn-start summary the rebuild performs runs after both,
so the row the boundary files states the new agent block and the turn that
continues reasons on it.

This pins that chain end to end through a real ``create_agent`` graph: write
knowledge out of band, change the prompt digest (the rebuild's due), continue
the thread, and read what the wire actually sent. What it does not verify:
that the summarizing model call itself is billed and admitted through Dalton's
ledger — that is the G4 inventory's question, answered against
``astream_ptc_workflow``'s admission path, not this graph.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from langchain.agents import create_agent
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ptc_agent.agent.main_state import MainAgentState
from ptc_agent.agent.middleware.compaction.compact import Summarizer
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware
from ptc_agent.agent.middleware.compaction import middleware as mw_module
from ptc_agent.agent.middleware.compaction.rebuild import TurnStartRebuilds
from ptc_agent.agent.middleware.runtime_context import tail as tail_module
from ptc_agent.agent.middleware.runtime_context.baseline import BaselineContextMiddleware
from ptc_agent.agent.middleware.runtime_context.carrier import Carrier
from ptc_agent.agent.middleware.runtime_context.frozen_prompt import SystemPinMiddleware
from ptc_agent.agent.middleware.runtime_context.tail import RowCarrierMiddleware
from ptc_agent.agent.middleware.reasoning_compat import ReasoningCompatibilityMiddleware
from ptc_agent.agent.middleware.tool_surface import ToolSurfaceMiddleware
from ptc_agent.agent.prompts import release
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge import reconcile
from src.dalton.knowledge.paths import AGENT_MD
from src.dalton.middleware.reconcile import DaltonReconcileMiddleware
from tests.unit.ptc_agent.agent.prefix_stability import (
    Wire,
    anthropic_model,
    is_compaction_request,
    reply,
)

THRESHOLD = 10_000
THREAD = "t-reconcile-rebuild"
CFG = {"configurable": {"thread_id": THREAD, "user_id": "u-1"}}
WS = "ws-reconcile"
USER = "user-1"
V1_MARKER = "V1-立场：旧观点一句话"
V2_MARKER = "V2-立场：知识写入后的新观点"


class _RecordingBackend:
    """The workspace filesystem: a dict, with agent.md writes on the record."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.events: list[tuple[str, str]] = []

    async def awrite_text(self, path: str, text: str) -> None:
        self.files[path] = text
        if path == AGENT_MD:
            self.events.append(("write_agent_md", text))

    async def aread_text(self, path: str) -> str | None:
        return self.files.get(path)


def _session(backend: _RecordingBackend) -> MagicMock:
    """A workspace session whose sandbox reads agent.md straight from the backend."""

    async def _read(path: str) -> str | None:
        text = backend.files.get("agent.md")
        backend.events.append(("read_agent_md", text or ""))
        return text

    session = MagicMock()
    session.sandbox.normalize_path = MagicMock(side_effect=lambda p: f"/home/workspace/{p}")
    session.sandbox.aread_file_text = MagicMock(side_effect=_read)
    session.take_agent_md_writer = MagicMock(return_value=None)
    session.conversation_id = WS
    return session


@pytest.fixture
def knowledge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    backend = _RecordingBackend()

    def _latest(doc_key: str, *, valid_only: bool = False, written: bool = False):
        cand = [
            r
            for r in rows
            if r["doc_key"] == doc_key
            and (not valid_only or r["schema_valid"])
            and (not written or r["files_written_at"] is not None)
        ]
        return max(cand, key=lambda r: r["version_no"]) if cand else None

    async def list_all_latest_impl(workspace_id: str, *, valid_only: bool = True):
        keys = {r["doc_key"] for r in rows}
        out = {k: _latest(k, valid_only=valid_only) for k in keys}
        return {k: deepcopy(v) for k, v in out.items() if v is not None}

    async def mark_written_impl(knowledge_version_id: str) -> None:
        for r in rows:
            if r["knowledge_version_id"] == knowledge_version_id:
                r["files_written_at"] = datetime.now(timezone.utc)

    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, side_effect=list_all_latest_impl),
    )
    monkeypatch.setattr(
        db_knowledge,
        "mark_files_written",
        create_autospec(db_knowledge.mark_files_written, side_effect=mark_written_impl),
    )
    monkeypatch.setattr(
        db_verdicts,
        "list_verdicts",
        create_autospec(db_verdicts.list_verdicts, return_value=[]),
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_workspace_profile",
        create_autospec(reconcile.resolve_workspace_profile, return_value="company"),
    )
    return {"rows": rows, "backend": backend}


def _insert(knowledge: dict[str, Any], one_liner: str) -> None:
    """A knowledge version landing out of band (an import, another worker)."""
    knowledge["rows"].append(
        {
            "knowledge_version_id": f"kv-{len(knowledge['rows']) + 1}",
            "workspace_id": WS,
            "doc_key": "thesis",
            "version_no": len(knowledge["rows"]) + 1,
            "source": "import",
            "content": {
                "one_liner": one_liner,
                "house_view": {"stance": "bull", "conviction": "high"},
            },
            "content_sha256": "0" * 64,
            "warnings": [],
            "schema_valid": True,
            "edited_path": None,
            "files_written_at": None,
            "created_at": datetime(2026, 11, 4, 9, 0, len(knowledge["rows"]), tzinfo=timezone.utc),
        }
    )


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(mw_module, "get_stream_writer", lambda: lambda payload: None)
    # Rows ride a user-role reminder on the routes Dalton deploys.
    monkeypatch.setattr(tail_module, "resolve_carrier", lambda model: Carrier("reminder"))
    # cl100k_base fetches its BPE file on first use; the blocked sockets of
    # the unit suite refuse that on a cold cache.
    from types import SimpleNamespace

    from ptc_agent.agent.middleware.compaction import utils as compaction_utils

    fake = SimpleNamespace(encode=lambda text: [0] * (len(text) // 4 + 1))
    monkeypatch.setattr(compaction_utils, "_get_tiktoken_encoder", lambda: fake)


@tool("get_data")
def get_data(query: str) -> str:
    """Look something up (stub)."""
    return f"data for {query}"


def _agent(saver: InMemorySaver, system: str, knowledge: dict[str, Any]):
    # The agent's own order (src/ptc_agent/agent/agent.py): compaction outside
    # every row writer, reconcile directly ahead of the baseline that reads
    # agent.md, the row carrier and the pin inside.
    return create_agent(
        model=anthropic_model(),
        tools=[get_data],
        system_prompt=system,
        middleware=[
            CompactionMiddleware(
                Summarizer(GenericFakeChatModel(messages=iter([])), limit=100_000),
                token_threshold=THRESHOLD,
                keep_tokens=THRESHOLD // 3,
                claude_keep_tokens=THRESHOLD // 3,
            ).with_turn_start_rebuilds(TurnStartRebuilds(0)),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            ToolSurfaceMiddleware(),
            DaltonReconcileMiddleware(
                workspace_id=WS,
                backend=knowledge["backend"],
                user_id=USER,
                storage_owner_id=USER,
            ),
            BaselineContextMiddleware(session=_session(knowledge["backend"]), guidance="lean"),
            RowCarrierMiddleware(),
            SystemPinMiddleware(),
            ReasoningCompatibilityMiddleware(),
        ],
        checkpointer=saver,
        state_schema=MainAgentState,
    )


def _measured(message: AIMessage, input_tokens: int) -> AIMessage:
    message.usage_metadata = {"input_tokens": input_tokens, "output_tokens": 50, "total_tokens": input_tokens + 50}
    return message


def _compaction_reply() -> AIMessage:
    return AIMessage(
        content=[{"type": "compaction", "content": "The thread so far, summarized.", "signature": "cmp-1"}],
        id="msg_cmp",
        response_metadata={"model_name": "claude-opus-5-5", "stop_reason": "compaction"},
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )


def _system_text(payload: dict) -> str:
    return json.dumps(payload.get("system"), ensure_ascii=False)


@pytest.mark.asyncio
async def test_knowledge_written_between_turns_reaches_the_model_through_the_turn_start_rebuild(
    knowledge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write knowledge → trigger the rebuild → the turn that continues reasons
    on the new agent_block. Claude's thinking is bound to the prompt prefix,
    so the frozen baseline block stands byte-identical and the new agent_block
    reaches the model as the row that states the changed agent.md section
    whole — from the rebuild's own turn on. What reconcile ahead of the
    baseline guarantees is that the boundary read of that turn already saw the
    reconciled agent.md, so the row it files carries the new block; had the
    baseline read first, the thread would state the old content and keep the
    new one waiting another turn."""
    backend: _RecordingBackend = knowledge["backend"]

    # Seed: one written thesis version, applied once out of band so agent.md
    # holds its block before the thread's first turn.
    _insert(knowledge, V1_MARKER)
    await reconcile.apply_pending(WS, backend, user_id=USER, skip_access_check=True)
    assert V1_MARKER in backend.files[AGENT_MD]

    wire = Wire(
        [
            _measured(reply(1, text="Noted the standing view."), THRESHOLD + 500),
            _compaction_reply(),
            reply(2, text="Understood, going on."),
            reply(3, text="Working from the new view."),
        ]
    ).install(monkeypatch)

    saver = InMemorySaver()
    await _agent(saver, "Prompt A.", knowledge).ainvoke({"messages": [HumanMessage("Where do we stand?")]}, CFG)

    # The knowledge write lands while the thread is idle, and the deploy
    # changes the prompt sources: the next turn start is due a rebuild.
    _insert(knowledge, V2_MARKER)
    monkeypatch.setattr(release, "source_digest", lambda: "the b12 deploy")
    await _agent(saver, "Prompt B.", knowledge).ainvoke({"messages": [HumanMessage("Go on.")]}, CFG)
    # The continuing turn: its boundary re-freezes the epoch on the summary
    # the rebuild's turn installed.
    await _agent(saver, "Prompt B.", knowledge).ainvoke({"messages": [HumanMessage("And the view?")]}, CFG)

    summary, summarized_turn, after = wire.payloads[1], wire.payloads[2], wire.payloads[3]
    assert is_compaction_request(summary), "the due turn start summarizes first"

    # Reconcile ran before the rebuild turn's boundary read: every read from
    # that point on already sees the reconciled agent.md.
    writes = [i for i, (kind, _) in enumerate(backend.events) if kind == "write_agent_md"]
    reads = [i for i, (kind, _) in enumerate(backend.events) if kind == "read_agent_md"]
    assert writes, "apply_pending rewrote agent.md at the turn start"
    assert any(V2_MARKER in backend.events[i][1] for i in writes), "the write carries the new block"
    reads_after = [i for i in reads if i > writes[-1]]
    assert reads_after and all(V2_MARKER in backend.events[i][1] for i in reads_after), (
        "every agent.md read after the reconcile sees the new block"
    )
    first_read = min(reads)
    assert V1_MARKER in backend.events[first_read][1], "the first turn froze the old block"

    # The turn that summarized and the one that continues run on the new
    # prompt. The frozen block stands (Claude's thinking is bound to the
    # prefix, so the system keeps the old block byte-identical), and the new
    # agent_block reaches the model as the row that states the changed
    # agent.md section whole — from the rebuild's own turn on.
    assert "Prompt B." in _system_text(summarized_turn)
    assert V1_MARKER in _system_text(summarized_turn)
    assert V2_MARKER in json.dumps(summarized_turn.get("messages"), ensure_ascii=False)
    assert "Prompt B." in _system_text(after)
    assert V2_MARKER in json.dumps(after.get("messages"), ensure_ascii=False)
    assert V2_MARKER not in json.dumps(wire.payloads[0], ensure_ascii=False)
    assert V2_MARKER not in json.dumps(summary, ensure_ascii=False)
    assert V1_MARKER in _system_text(wire.payloads[0]), "the first turn really ran on the old block"
