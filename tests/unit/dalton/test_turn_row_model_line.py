"""Model identity in Dalton after adopting upstream <model> + model_changed
(replacing the retired turn-row-model-line hook), plus single-computer
CopyFile exclusion check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ptc_agent.agent.agent import PTCAgent
from ptc_agent.agent.middleware.runtime_context import (
    BaselineContextMiddleware,
    TurnContext,
    TurnContextMiddleware,
)
from ptc_agent.agent.middleware.runtime_context.durable import runtime_update_from_message
from ptc_agent.agent.turn import TurnModel
from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.mcp_registry import MCPRegistry
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import PTCSandbox
from ptc_agent.core.sandbox.asset_gate import AssetGate

NOW = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)


def _baseline_middleware(label_box: dict[str, str]) -> BaselineContextMiddleware:
    return BaselineContextMiddleware(
        blocks={"model": lambda _state: label_box["model"]},
    )


async def _rendered_baseline(mw: BaselineContextMiddleware, state: dict) -> str:
    captured: list[SystemMessage] = []

    async def _handler(req):
        captured.append(req.system_message)
        return SimpleNamespace(result=[])

    req = SimpleNamespace(
        state=state,
        system_message=SystemMessage(content="static"),
        model=None,
        override=lambda **kw: SimpleNamespace(
            state=state,
            system_message=kw.get("system_message", SystemMessage(content="static")),
            model=None,
        ),
    )
    await mw.awrap_model_call(req, _handler)
    content = captured[0].content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


@pytest.mark.asyncio
async def test_model_block_covers_designated_default_switch_and_fallback_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", raising=False)

    # 1. Fixed/designated model (e.g., automation llm_model)
    client_astra = SimpleNamespace(
        metadata={"manifest_model": "gpt-6.2-astra", "reasoning_effort": "high"}
    )
    fixed_label = TurnModel(client=client_astra, name="gpt-6.2-astra", guidance="lean").label
    assert "gpt-6.2-astra" in fixed_label
    assert "reasoning effort high" in fixed_label

    box = {"model": fixed_label}
    mw = _baseline_middleware(box)
    state = await mw.abefore_agent({}, None) or {}
    text = await _rendered_baseline(mw, state)
    assert f"<model>\nYou run on {fixed_label}." in text
    assert "server-designated model for the thread" in text
    assert "fallback" in text
    assert "The user picks the model." in text
    assert "la models list" not in text

    # 2. Switch between turns (or account default -> another model) emits model_changed
    default_label = TurnModel(
        client=SimpleNamespace(metadata={"manifest_model": "gpt-6.2-sol"}),
        name="gpt-6.2-sol",
        guidance="lean",
    ).label
    box["model"] = default_label
    update = await mw.abefore_agent(state, None) or {}
    rows = [runtime_update_from_message(m) for m in update.get("messages", [])]
    assert [r.kind for r in rows] == ["model_changed"]
    assert f"From this turn on you run on {default_label}." in rows[0].text


@pytest.mark.asyncio
async def test_model_block_includes_la_only_when_cli_tokens_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", "true")
    mw = _baseline_middleware({"model": "GPT-6.2 Sol (gpt-6.2-sol)"})
    state = await mw.abefore_agent({}, None) or {}
    text = await _rendered_baseline(mw, state)
    assert "`la models list`" in text


@pytest.mark.asyncio
async def test_retired_turn_row_model_line_is_absent() -> None:
    assert not hasattr(TurnContext(), "model_key")
    written = await TurnContextMiddleware(now=NOW).abefore_agent({})
    assert written is not None
    assert len(written["messages"]) == 1
    assert isinstance(written["messages"][0], HumanMessage)
    assert "服务端为本回合指定的模型" not in written["messages"][0].content


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
def test_single_computer_agent_has_no_copy_file_tool(role: str) -> None:
    """Single-computer Docker users do not receive CopyFile; CoS keeps manage_workspaces."""
    built: list[dict] = []

    def _capture(model, **kw):
        built.append(kw)
        fake_graph = create_autospec(GenericFakeChatModel, instance=True)
        fake_graph.with_config = lambda *_a, **_kw: fake_graph
        return fake_graph

    config = AgentConfig(
        llm=LLMConfig(name="gpt-6.2-sol"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    client = GenericFakeChatModel(messages=iter([]))
    compaction_llm = GenericFakeChatModel(messages=iter([]))
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.assets = AssetGate()
    sandbox.config = config
    sandbox.livefs = None
    sandbox.workspace.return_value = SandboxLayout("/home/workspace").for_workspace("ws")
    mcp_registry = create_autospec(MCPRegistry, instance=True)
    mcp_registry.get_all_tools.return_value = {}
    mcp_registry.server_configs = {}

    with (
        patch.object(AgentConfig, "get_llm_client", return_value=client),
        patch(
            "ptc_agent.agent.middleware.compaction.compact.create_llm",
            return_value=compaction_llm,
        ),
        patch("ptc_agent.agent.agent.create_agent", side_effect=_capture),
    ):
        PTCAgent(config).create_agent(
            llm=client,
            thread_id="t-single-comp",
            sandbox=sandbox,
            mcp_registry=mcp_registry,
            user_id="u-1",
            tool_summary="",
            role=role,  # type: ignore[arg-type]
            computers=None,
        )

    tool_names = {t.name for t in built[0]["tools"]}
    assert "CopyFile" not in tool_names
    if role == "chief_of_staff":
        assert "manage_workspaces" in tool_names


def test_single_computer_roster_gives_no_copy_file_via_turn_computers() -> None:
    """turn_computers returns None for a 1-computer roster, so CoS has no CopyFile."""
    from ptc_agent.core.session import Session
    from src.server.services.computer_reach import turn_computers

    session = create_autospec(Session, instance=True)
    reach = turn_computers(
        "u-1",
        "c-1",
        session,
        [{"computer_id": "c-1", "name": "Docker", "kind": "docker"}],
    )
    assert reach is None

    built: list[dict] = []

    def _capture(model, **kw):
        built.append(kw)
        fake_graph = create_autospec(GenericFakeChatModel, instance=True)
        fake_graph.with_config = lambda *_a, **_kw: fake_graph
        return fake_graph

    config = AgentConfig(
        llm=LLMConfig(name="gpt-6.2-sol"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    client = GenericFakeChatModel(messages=iter([]))
    compaction_llm = GenericFakeChatModel(messages=iter([]))
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.assets = AssetGate()
    sandbox.config = config
    sandbox.livefs = None
    sandbox.workspace.return_value = SandboxLayout("/home/workspace").for_workspace("ws")
    mcp_registry = create_autospec(MCPRegistry, instance=True)
    mcp_registry.get_all_tools.return_value = {}
    mcp_registry.server_configs = {}

    with (
        patch.object(AgentConfig, "get_llm_client", return_value=client),
        patch(
            "ptc_agent.agent.middleware.compaction.compact.create_llm",
            return_value=compaction_llm,
        ),
        patch("ptc_agent.agent.agent.create_agent", side_effect=_capture),
    ):
        PTCAgent(config).create_agent(
            llm=client,
            thread_id="t-single-roster",
            sandbox=sandbox,
            mcp_registry=mcp_registry,
            user_id="u-1",
            tool_summary="",
            role="chief_of_staff",
            computers=reach,
        )

    tool_names = {t.name for t in built[0]["tools"]}
    assert "CopyFile" not in tool_names
    assert "manage_workspaces" in tool_names
