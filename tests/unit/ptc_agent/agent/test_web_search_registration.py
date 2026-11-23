"""Tests for on-demand WebSearch registration across main agent, subagents, and market insights.

Core profile without any search provider API key omits ``WebSearch`` from main
agents (``analyst`` and ``chief_of_staff``), subagents, and the market insight
agent (removing WebSearch news guidance) while keeping ``WebFetch`` bound.
Providing a search key registers ``WebSearch`` again, avoids duplicate subagent
search construction when parent already fell back to the same provider, and
Everflow profile keeps its existing tool set and prompts unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from langchain.agents import create_agent as langchain_create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from ptc_agent.agent.agent import PTCAgent, task_middleware as real_task_middleware
from ptc_agent.agent.insight_graph import (
    INSIGHT_RESEARCH_PROMPT,
    background_client as real_insight_background_client,
    build_insight_graph,
    insight_tools,
)
from ptc_agent.agent.middleware.compaction import compact as compact_mod
from ptc_agent.agent.subagents.compiler import SubagentCompiler
from ptc_agent.agent.subagents.definition import SubagentDefinition
from ptc_agent.config.agent import AgentConfig
from ptc_agent.config.file_utils import load_yaml_config
from ptc_agent.config.loaders import load_from_dict
from ptc_agent.core.mcp_registry import MCPRegistry
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
from ptc_agent.core.sandbox.livefs_mount import MountHandle
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.config.core import clear_config_cache
from src.dalton.settings import get_dalton_config
from src.dalton.web_search_spec import adapt_insight_user_prompt
from src.tools.web.manifest import get_web_providers
from src.tools.web.search import get_web_search_tool

REPO_ROOT = Path(__file__).resolve().parents[4]
_ORIG_GET_LLM_CLIENT = AgentConfig.get_llm_client
_ORIG_COMPACT_CREATE_LLM = compact_mod.create_llm

_SEARCH_ENV_KEYS = (
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "BOCHA_API_KEY",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
)


def _reset_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str,
    dalton_profile: str | None = None,
    search_keys: dict[str, str] | None = None,
) -> None:
    for key in _SEARCH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, val in (search_keys or {}).items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is None:
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    clear_config_cache()
    get_dalton_config.cache_clear()
    get_web_providers.cache_clear()


def _load_real_agent_config() -> AgentConfig:
    raw = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
    config = load_from_dict(raw)
    config.config_file_dir = REPO_ROOT
    return config


def _build_and_capture_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str = "analyst",
) -> tuple[set[str], dict[str, set[str]]]:
    import ptc_agent.agent.agent as agent_mod

    config = _load_real_agent_config()

    fake_llm = GenericFakeChatModel(messages=iter([]))
    captured_main: list[dict[str, Any]] = []
    captured_subagents: list[list[dict[str, Any]]] = []

    def _capture_create_agent(_model: Any, **kwargs: Any) -> Any:
        captured_main.append(kwargs)
        graph = create_autospec(langchain_create_agent(fake_llm, tools=[]), instance=True)
        graph.with_config.return_value = graph
        return graph

    def _spy_task_middleware(subagents: Any, *args: Any, **kwargs: Any) -> Any:
        captured_subagents.append(list(subagents))
        return real_task_middleware(subagents, *args, **kwargs)

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.config = config.to_core_config()
    sandbox.working_dir = "/home/workspace"
    sandbox.layout = SandboxLayout(root="/home/workspace")
    sandbox.workspace.return_value = WorkspaceLayout(root="/home/workspace")
    sandbox.livefs = create_autospec(MountHandle, instance=True)
    sandbox.mounts_livefs = True
    sandbox.history = None
    sandbox.skills_manifest = None

    mcp_registry = create_autospec(MCPRegistry, instance=True)
    mcp_registry.connectors = {}
    mcp_registry.tools_by_server = {}
    mcp_registry.get_all_tools.return_value = {}

    monkeypatch.setattr(
        AgentConfig,
        "get_llm_client",
        create_autospec(_ORIG_GET_LLM_CLIENT, return_value=fake_llm),
    )
    monkeypatch.setattr(
        compact_mod,
        "create_llm",
        create_autospec(_ORIG_COMPACT_CREATE_LLM, return_value=fake_llm),
    )
    monkeypatch.setattr(
        agent_mod,
        "create_agent",
        create_autospec(langchain_create_agent, side_effect=_capture_create_agent),
    )
    monkeypatch.setattr(
        agent_mod,
        "task_middleware",
        create_autospec(real_task_middleware, side_effect=_spy_task_middleware),
    )

    PTCAgent(config).create_agent(
        llm=fake_llm,
        thread_id="11111111-1111-1111-1111-111111111111",
        sandbox=sandbox,
        mcp_registry=mcp_registry,
        user_id="00000000-0000-0000-0000-000000000001",
        tool_summary="",
        role=role,  # type: ignore[arg-type]
    )

    main_tools = {t.name for t in captured_main[0]["tools"]}
    sub_tools = {
        str(spec["name"]): {t.name for t in spec.get("tools") or []}
        for spec in captured_subagents[0]
    }
    return main_tools, sub_tools


def _build_and_capture_insight(monkeypatch: pytest.MonkeyPatch) -> tuple[set[str], str]:
    from ptc_agent.agent import insight_graph as insight_mod

    config = _load_real_agent_config()
    fake_llm = GenericFakeChatModel(messages=iter([]))
    captured: list[dict[str, Any]] = []

    def _capture_create_agent(_model: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        graph = create_autospec(langchain_create_agent(fake_llm, tools=[]), instance=True)
        graph.with_config.return_value = graph
        return graph

    monkeypatch.setattr(
        insight_mod,
        "background_client",
        create_autospec(real_insight_background_client, return_value=fake_llm),
    )
    monkeypatch.setattr(
        compact_mod,
        "create_llm",
        create_autospec(_ORIG_COMPACT_CREATE_LLM, return_value=fake_llm),
    )
    monkeypatch.setattr(
        insight_mod,
        "create_agent",
        create_autospec(langchain_create_agent, side_effect=_capture_create_agent),
    )

    build_insight_graph(config)
    tool_names = {t.name for t in captured[0]["tools"]}
    assert tool_names == {t.name for t in insight_tools(config)}
    return tool_names, str(captured[0]["system_prompt"])


@pytest.mark.parametrize(
    ("app_env", "dalton_profile"),
    [("core", None), ("", "core")],
    ids=["APP_ENV=core", "DALTON_PROFILE=core"],
)
def test_core_without_search_keys_omits_web_search_and_keeps_web_fetch(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    dalton_profile: str | None,
) -> None:
    _reset_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)

    main_tools, sub_tools = _build_and_capture_tools(monkeypatch, role="analyst")
    assert "WebSearch" not in main_tools
    assert "WebFetch" in main_tools

    for sub_name in ("research", "general-purpose", "equity-analyst"):
        assert sub_name in sub_tools
        assert "WebSearch" not in sub_tools[sub_name]
        assert "WebFetch" in sub_tools[sub_name]

    cos_tools, cos_sub_tools = _build_and_capture_tools(monkeypatch, role="chief_of_staff")
    assert "WebSearch" not in cos_tools
    assert "WebFetch" in cos_tools
    for sub_name in ("research", "general-purpose"):
        assert "WebSearch" not in cos_sub_tools[sub_name]
        assert "WebFetch" in cos_sub_tools[sub_name]


@pytest.mark.parametrize(
    "search_keys",
    [
        {"TAVILY_API_KEY": "tvly-test-key"},
        {"SERPER_API_KEY": "serper-test-key"},
    ],
    ids=["tavily-key", "serper-key-fallback"],
)
def test_core_with_search_key_registers_web_search_for_main_and_subagents(
    monkeypatch: pytest.MonkeyPatch,
    search_keys: dict[str, str],
) -> None:
    _reset_env(monkeypatch, app_env="core", search_keys=search_keys)

    main_tools, sub_tools = _build_and_capture_tools(monkeypatch, role="analyst")
    assert "WebSearch" in main_tools
    assert "WebFetch" in main_tools

    for sub_name in ("research", "general-purpose", "equity-analyst"):
        assert "WebSearch" in sub_tools[sub_name]
        assert "WebFetch" in sub_tools[sub_name]


@pytest.mark.parametrize("app_env", ["dalton", ""])
def test_everflow_keeps_web_search_even_without_env_keys(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    _reset_env(monkeypatch, app_env=app_env)

    main_tools, sub_tools = _build_and_capture_tools(monkeypatch, role="analyst")
    assert "WebSearch" in main_tools
    assert "WebFetch" in main_tools
    for sub_name in ("research", "general-purpose", "equity-analyst"):
        assert "WebSearch" in sub_tools[sub_name]
        assert "WebFetch" in sub_tools[sub_name]


def test_subagent_compiler_skips_override_web_search_in_core_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_env(monkeypatch, app_env="core")
    config = _load_real_agent_config()

    spy_builder = create_autospec(get_web_search_tool)
    compiler = SubagentCompiler(
        config=config,
        tool_sets={"web_search": []},
        search_tool=spy_builder,
    )
    defn = SubagentDefinition(
        name="custom-searcher",
        description="test",
        custom_prompt="prompt",
        tools=["web_search"],
        search_provider="serper",
    )

    compiled = compiler.compile(defn)
    assert compiled["tools"] == []
    assert spy_builder.call_count == 0


def test_subagent_compiler_reuses_parent_resolved_provider_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grok 低 4: when parent already fell back to serper, subagent targeting serper makes no duplicate."""
    _reset_env(monkeypatch, app_env="core", search_keys={"SERPER_API_KEY": "serper-key"})
    config = _load_real_agent_config()
    assert config.search_api == "tavily"

    parent_search = get_web_search_tool(max_search_results=10, provider="serper")
    spy_builder = create_autospec(get_web_search_tool)
    compiler = SubagentCompiler(
        config=config,
        tool_sets={"web_search": [parent_search]},
        search_tool=spy_builder,
    )
    defn = SubagentDefinition(
        name="custom-serper",
        description="test",
        custom_prompt="prompt",
        tools=["web_search"],
        search_provider="serper",
    )

    compiled = compiler.compile(defn)
    assert spy_builder.call_count == 0
    assert compiled["tools"] == [parent_search]


def test_insight_graph_omits_web_search_and_prompt_in_core_without_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex 中 2: core market-insight agent omits WebSearch and WebSearch prompt guidance without keys."""
    _reset_env(monkeypatch, app_env="core")

    tools, prompt = _build_and_capture_insight(monkeypatch)
    assert "WebSearch" not in tools
    assert "WebFetch" in tools
    assert "get_sec_filing" in tools
    assert "get_quote" in tools
    assert "WebSearch" not in prompt

    adapted = adapt_insight_user_prompt("Curate market brief.", search_api="tavily")
    assert "WebSearch is not configured" in adapted


def test_insight_graph_keeps_web_search_in_core_with_key_and_in_everflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_env(monkeypatch, app_env="core", search_keys={"SERPER_API_KEY": "serper-key"})
    core_tools, core_prompt = _build_and_capture_insight(monkeypatch)
    assert "WebSearch" in core_tools
    assert "WebFetch" in core_tools
    assert core_prompt == INSIGHT_RESEARCH_PROMPT
    assert adapt_insight_user_prompt("Curate market brief.", search_api="tavily") == (
        "Curate market brief."
    )

    for app_env in ("dalton", ""):
        _reset_env(monkeypatch, app_env=app_env)
        ef_tools, ef_prompt = _build_and_capture_insight(monkeypatch)
        assert "WebSearch" in ef_tools
        assert "WebFetch" in ef_tools
        assert ef_prompt == INSIGHT_RESEARCH_PROMPT
        assert adapt_insight_user_prompt("Curate market brief.", search_api="gemini_grounding") == (
            "Curate market brief."
        )
