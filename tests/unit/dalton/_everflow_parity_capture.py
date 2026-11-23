"""Isolated subprocess capture worker for Everflow profile parity snapshots.

Running capture inside a fresh subprocess guarantees that:
- Host ``.env`` files are never loaded (``dotenv.load_dotenv`` is disabled
  before any ``src.*`` / ``ptc_agent.*`` import).
- Host shell env vars such as ``DALTON_TRANSCRIPTION_ENABLED`` or
  ``AUTH_USER_ID`` cannot leak into import-time singletons
  (``DALTON_SKILL_DEFINITIONS``, ``LOCAL_DEV_USER_ID``).
- Host ``~/.ptc-agent/skills`` is never read (empty ``HOME`` + explicit
  repo-only skill dirs).
- Data-map token estimation uses the deterministic offline fallback formula
  (``max(1, len(md) // 2)``) so snapshots never depend on local ``tiktoken``
  vocabulary cache or network access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

# Disable .env loading BEFORE importing any application modules so developer
# machine .env values (AUTH_USER_ID, DALTON_TRANSCRIPTION_ENABLED, etc.) never
# leak into the isolated parity capture.
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from langchain.agents import create_agent as langchain_create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.tools import tool

from ptc_agent.agent import agent as agent_mod
from ptc_agent.agent import graph as graph_mod
from ptc_agent.agent.agent import task_middleware as real_task_middleware
from ptc_agent.agent.insight_graph import (
    INSIGHT_RESEARCH_PROMPT,
    build_insight_graph,
    insight_tools,
)
from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
    PrebuiltWorkflowRegistry,
)
from ptc_agent.agent.middleware.compaction import compact as compact_mod
from ptc_agent.agent.middleware.direct_mcp import METADATA_KEY, DirectToolSet
from ptc_agent.agent.middleware.runtime_context.baseline import (
    BaselineContextMiddleware,
)
from ptc_agent.agent.middleware.runtime_context.epoch import Workspace
from ptc_agent.agent.middleware.runtime_context.harness_blocks import (
    HARNESS_BLOCKS,
    MCP_SERVERS_BLOCK,
)
from ptc_agent.agent.middleware.runtime_context.templates import render_template
from ptc_agent.agent.middleware.skills.content import load_skill_content, resolve_skill_dir
from ptc_agent.agent.middleware.skills.dalton_registry import (
    DALTON_SKILL_DEFINITIONS,
    DALTON_SKILLS_DIR,
)
from ptc_agent.agent.middleware.skills.registry import (
    get_skill_registry,
    list_skills,
    skills_outside_sandbox,
)
from ptc_agent.agent.prompts.loader import init_loader, reset_loader
from ptc_agent.config.agent import AgentConfig
from ptc_agent.config.file_utils import load_yaml_config
from ptc_agent.config.loaders import load_from_dict
from ptc_agent.core.mcp_registry import MCPRegistry
from ptc_agent.core.mcp_schema import MCPToolInfo
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox.livefs_mount import MountHandle
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.session import Session
from src.config.core import clear_config_cache, load_infrastructure_config
from src.dalton import access_tiers, sharing
from src.dalton.data_map import (
    builder as data_map_builder,
    get_or_build_data_map_markdown,
    render_data_map_markdown,
    render_static_data_map_markdown,
    set_turn_data_map,
)
from src.dalton.database import data_map as db_data_map
from src.dalton.settings import get_dalton_config
from src.server.database import user_vault_secrets
from src.tools.web.fetch import get_fetch_chain
from src.tools.web.manifest import get_web_providers

FIXED_UTC = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
FIXED_THREAD_ID = "11111111-1111-1111-1111-111111111111"
FIXED_USER_ID = "00000000-0000-0000-0000-000000000001"
FIXED_WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"
FIXED_COMPUTER_ID = "33333333-3333-3333-3333-333333333333"
FIXED_SANDBOX_ROOT = "/home/workspace"

_ORIG_GET_LLM_CLIENT = AgentConfig.get_llm_client
_ORIG_COMPACT_CREATE_LLM = compact_mod.create_llm
_ORIG_BUILD_INSIGHT_CREATE_AGENT = None

# Allowed extensions for assembled text snapshots (skip dotfiles & junk files).
_SKILL_REFERENCE_EXTENSIONS = frozenset({".md", ".json", ".yaml", ".yml"})
_WORKFLOW_EXTENSIONS = frozenset({".js", ".json"})

_FIXED_HEALTH_MAP: dict[str, dict[str, Any]] = {
    "researchdata:alphaengine": {"status": "available", "latest_date": "2026-11-19"},
    "researchdata:guidepoint": {"status": "available", "latest_date": "2026-11-18"},
    "researchdata:wiki": {"status": "available", "latest_date": "2026-11-20"},
    "researchdata:sec": {"status": "available", "latest_date": "2026-11-19"},
    "x_search": {"status": "available", "latest_date": "2026-11-20"},
}
_FIXED_CONV_STATS: dict[str, Any] = {
    "card_count": 42842,
    "latest_date": "2026-11-18",
}


class _FixedDatetime(datetime):
    """Deterministic ``datetime`` replacement returning ``FIXED_UTC`` from ``now()``."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:
            return FIXED_UTC.replace(tzinfo=None)
        return FIXED_UTC.astimezone(tz)


def _offline_token_estimate(markdown: str) -> int:
    """Deterministic offline token estimate matching ``builder.py:62`` fallback."""
    return max(1, len(markdown) // 2)


def _has_dot_part(rel_path: Path) -> bool:
    return any(part.startswith(".") for part in rel_path.parts)


def _normalize_repo_path(value: Any) -> Any:
    root_str = str(REPO_ROOT)
    if isinstance(value, str):
        return value.replace(root_str, "<REPO_ROOT>")
    if isinstance(value, list):
        return [_normalize_repo_path(item) for item in value]
    if isinstance(value, dict):
        return {k: _normalize_repo_path(v) for k, v in value.items()}
    return value


def _make_fixed_mcp_tools(config: AgentConfig) -> dict[str, list[MCPToolInfo]]:
    """Deterministic MCPToolInfo fixtures for enabled servers so formatter renders real docs."""
    catalog: dict[str, list[MCPToolInfo]] = {
        "price_data": [
            MCPToolInfo(
                name="get_stock_data",
                description="Fetch OHLCV historical prices and quote metrics for a ticker.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"},
                        "interval": {
                            "type": "string",
                            "description": "Bar interval",
                            "default": "1day",
                        },
                    },
                    "required": ["symbol"],
                },
                server_name="price_data",
            ),
        ],
        "fundamentals": [
            MCPToolInfo(
                name="get_financial_statements",
                description="Retrieve quarterly or annual financial statements.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"},
                        "period": {"type": "string", "default": "annual"},
                    },
                    "required": ["symbol"],
                },
                server_name="fundamentals",
            ),
        ],
        "dalton_sec": [
            MCPToolInfo(
                name="get_sec_company_facts",
                description="Read structured SEC EDGAR XBRL facts for a US issuer.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string", "description": "US ticker symbol"},
                    },
                    "required": ["ticker"],
                },
                server_name="dalton_sec",
            ),
        ],
        "researchdata": [
            MCPToolInfo(
                name="search_research",
                description="Search institutional research documents, transcripts, and notes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                    },
                    "required": ["query"],
                },
                server_name="researchdata",
            ),
        ],
    }
    enabled_names = {s.name for s in config.mcp.servers if s.enabled}
    return {name: tools for name, tools in catalog.items() if name in enabled_names}


def _make_direct_mcp_double() -> Any:
    """Create a deterministic ``DirectToolSet`` double with one stamped direct tool."""

    @tool("direct_market_note")
    def _direct_market_note(symbol: str) -> str:
        """Read a directly-bound market note for a ticker."""
        return symbol

    _direct_market_note.metadata = {
        METADATA_KEY: {
            "server": "researchdata",
            "tool": "direct_market_note",
            "approval": False,
            "sandboxed": True,
        }
    }

    direct_mcp = create_autospec(DirectToolSet, instance=True)
    direct_mcp.tools = [_direct_market_note]
    direct_mcp.check.return_value = None
    return direct_mcp


def _build_fixed_cached_data_map() -> str:
    """Build deterministic cached PostgreSQL data-map markdown (with x_search + team memory)."""
    return render_data_map_markdown(
        catalog_routes=None,
        handoff_routes=None,
        health_map=_FIXED_HEALTH_MAP,
        conversation_stats=_FIXED_CONV_STATS,
        x_search_enabled=True,
    )


async def _capture_production_graph_surface(
    config: AgentConfig,
    *,
    role: str,
    cached_data_map_md: str,
) -> dict[str, Any]:
    """Build agent through production ``build_ptc_graph_with_session`` and capture outputs."""
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

    core_cfg = config.to_core_config()
    fixed_mcp_tools = _make_fixed_mcp_tools(config)

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.config = core_cfg
    sandbox.working_dir = FIXED_SANDBOX_ROOT
    sandbox.layout = SandboxLayout(root=FIXED_SANDBOX_ROOT)
    sandbox.workspace.return_value = WorkspaceLayout(root=FIXED_SANDBOX_ROOT)
    sandbox.livefs = create_autospec(MountHandle, instance=True)
    sandbox.mounts_livefs = True
    sandbox.history = None
    sandbox.skills_manifest = None
    sandbox.vault_secrets = {}

    mcp_registry = create_autospec(MCPRegistry, instance=True)
    mcp_registry.config = core_cfg
    mcp_registry.connectors = {}
    mcp_registry.tools_by_server = fixed_mcp_tools
    mcp_registry.get_all_tools.return_value = fixed_mcp_tools

    session = create_autospec(Session, instance=True)
    session.sandbox = sandbox
    session.mcp_registry = mcp_registry
    session.mcp_tool_summary = None  # Force tool_summary=None -> real registry formatting
    session.computer_id = FIXED_COMPUTER_ID
    session.note_agent_md_write = create_autospec(Session.note_agent_md_write)

    project = ProjectContext(
        workspace_id=FIXED_WORKSPACE_ID,
        dir_name=None,
    )
    direct_mcp = _make_direct_mcp_double()

    workspace_obj = Workspace(
        name="Home" if role == "chief_of_staff" else "Everflow Parity Workspace",
        description="Deterministic workspace for parity lock.",
        computer="Local",
        machine="",
        root="",
    )

    # Save originals before patching for this capture call
    orig_dt = agent_mod.datetime
    orig_create_agent = agent_mod.create_agent
    orig_task_mw = agent_mod.task_middleware
    orig_fetch_counts = graph_mod.fetch_user_data_counts
    orig_ws_naming = graph_mod._read_workspace_naming
    orig_harness = graph_mod._read_harness_blocks
    orig_get_tier = access_tiers.get_user_tier
    orig_team_mem = sharing.resolve_turn_team_memory_enabled
    orig_secrets = user_vault_secrets.get_user_secrets_decrypted
    orig_get_cached_dm = db_data_map.get_cached_data_map

    try:
        agent_mod.datetime = _FixedDatetime  # type: ignore[misc]
        AgentConfig.get_llm_client = create_autospec(  # type: ignore[method-assign]
            _ORIG_GET_LLM_CLIENT, return_value=fake_llm
        )
        compact_mod.create_llm = create_autospec(  # type: ignore[assignment]
            _ORIG_COMPACT_CREATE_LLM, return_value=fake_llm
        )
        agent_mod.create_agent = create_autospec(  # type: ignore[assignment]
            langchain_create_agent, side_effect=_capture_create_agent
        )
        agent_mod.task_middleware = create_autospec(  # type: ignore[assignment]
            real_task_middleware, side_effect=_spy_task_middleware
        )
        graph_mod.fetch_user_data_counts = create_autospec(  # type: ignore[assignment]
            orig_fetch_counts,
            return_value={
                "portfolio_count": 0,
                "watchlist_summary": "0:0",
                "prefs_set": False,
                "watchlist_symbols": [],
            },
        )
        graph_mod._read_workspace_naming = create_autospec(  # type: ignore[assignment]
            orig_ws_naming,
            return_value=workspace_obj,
        )
        graph_mod._read_harness_blocks = create_autospec(  # type: ignore[assignment]
            orig_harness,
            return_value=({}, None),
        )
        access_tiers.get_user_tier = create_autospec(  # type: ignore[assignment]
            orig_get_tier,
            return_value="internal",
        )
        sharing.resolve_turn_team_memory_enabled = create_autospec(  # type: ignore[assignment]
            orig_team_mem,
            return_value=True,
        )
        user_vault_secrets.get_user_secrets_decrypted = create_autospec(  # type: ignore[assignment]
            orig_secrets,
            return_value={},
        )
        db_data_map.get_cached_data_map = create_autospec(  # type: ignore[assignment]
            orig_get_cached_dm,
            return_value={"rendered_markdown": cached_data_map_md},
        )

        set_turn_data_map(None)
        await graph_mod.build_ptc_graph_with_session(
            session=session,
            config=config,
            user_id=FIXED_USER_ID,
            owner_id=FIXED_USER_ID,
            thread_id=FIXED_THREAD_ID,
            project=project,
            direct_mcp=direct_mcp,
            role=role,  # type: ignore[arg-type]
        )
    finally:
        agent_mod.datetime = orig_dt  # type: ignore[misc]
        AgentConfig.get_llm_client = _ORIG_GET_LLM_CLIENT  # type: ignore[method-assign]
        compact_mod.create_llm = _ORIG_COMPACT_CREATE_LLM
        agent_mod.create_agent = orig_create_agent
        agent_mod.task_middleware = orig_task_mw
        graph_mod.fetch_user_data_counts = orig_fetch_counts
        graph_mod._read_workspace_naming = orig_ws_naming
        graph_mod._read_harness_blocks = orig_harness
        access_tiers.get_user_tier = orig_get_tier
        sharing.resolve_turn_team_memory_enabled = orig_team_mem
        user_vault_secrets.get_user_secrets_decrypted = orig_secrets
        db_data_map.get_cached_data_map = orig_get_cached_dm

    assert len(captured_main) == 1
    assert len(captured_subagents) == 1
    main_kwargs = captured_main[0]
    subagent_specs = captured_subagents[0]

    main_prompt = str(main_kwargs["system_prompt"])
    assert "MagicMock" not in main_prompt and "AsyncMock" not in main_prompt

    # Render the baseline <mcp-servers> block from the actual BaselineContextMiddleware
    baseline_mws = [
        m for m in (main_kwargs.get("middleware") or []) if isinstance(m, BaselineContextMiddleware)
    ]
    assert len(baseline_mws) == 1
    mcp_summary_content = baseline_mws[0]._blocks[MCP_SERVERS_BLOCK]({})
    baseline_mcp_servers_block = render_template(
        HARNESS_BLOCKS[MCP_SERVERS_BLOCK].template,
        content=mcp_summary_content,
    )
    assert "<mcp-servers>" in baseline_mcp_servers_block
    assert "MagicMock" not in baseline_mcp_servers_block

    subagent_tools: dict[str, list[str]] = {}
    subagent_prompts: dict[str, str] = {}
    for spec in sorted(subagent_specs, key=lambda s: str(s["name"])):
        name = str(spec["name"])
        prompt = str(spec["system_prompt"])
        assert "MagicMock" not in prompt and "AsyncMock" not in prompt
        subagent_tools[name] = sorted(t.name for t in spec.get("tools") or [])
        subagent_prompts[name] = prompt

    return {
        "main_tools": sorted(t.name for t in main_kwargs.get("tools") or []),
        "main_prompt": main_prompt,
        "baseline_mcp_servers": baseline_mcp_servers_block,
        "subagent_tools": subagent_tools,
        "subagent_prompts": subagent_prompts,
    }


def _capture_insight_surface(config: AgentConfig) -> dict[str, Any]:
    """Capture Market Insight tools and system prompt for Everflow parity."""
    from ptc_agent.agent import insight_graph as insight_mod

    fake_llm = GenericFakeChatModel(messages=iter([]))
    captured: list[dict[str, Any]] = []

    def _capture_insight(_model: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        graph = create_autospec(langchain_create_agent(fake_llm, tools=[]), instance=True)
        graph.with_config.return_value = graph
        return graph

    orig_create = insight_mod.create_agent
    orig_bg = insight_mod.background_client
    try:
        insight_mod.create_agent = create_autospec(  # type: ignore[assignment]
            langchain_create_agent, side_effect=_capture_insight
        )
        insight_mod.background_client = create_autospec(  # type: ignore[assignment]
            orig_bg, return_value=fake_llm
        )
        compact_mod.create_llm = create_autospec(  # type: ignore[assignment]
            _ORIG_COMPACT_CREATE_LLM, return_value=fake_llm
        )
        build_insight_graph(config)
    finally:
        insight_mod.create_agent = orig_create
        insight_mod.background_client = orig_bg
        compact_mod.create_llm = _ORIG_COMPACT_CREATE_LLM

    assert len(captured) == 1
    tool_names = sorted(t.name for t in captured[0]["tools"])
    assert tool_names == sorted(t.name for t in insight_tools(config))
    return {
        "tools": tool_names,
        "system_prompt": str(captured[0]["system_prompt"]),
        "matches_default_prompt": str(captured[0]["system_prompt"]) == INSIGHT_RESEARCH_PROMPT,
    }


def _collect_dalton_skills_visibility() -> dict[str, dict[str, Any]]:
    disk_skills = sorted(
        p.name for p in DALTON_SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    assert len(disk_skills) == 51, f"Expected 51 Dalton skills on disk, got {len(disk_skills)}"
    assert sorted(DALTON_SKILL_DEFINITIONS) == disk_skills

    enabled_registry = get_skill_registry()
    listed_names = {entry["name"] for entry in list_skills()}
    outside = skills_outside_sandbox()

    visibility: dict[str, dict[str, Any]] = {}
    for name in disk_skills:
        defn = DALTON_SKILL_DEFINITIONS[name]
        visibility[name] = {
            "enabled_in_registry": name in enabled_registry,
            "hidden": bool(defn.hidden),
            "in_list_skills": name in listed_names,
            "in_sandbox": name not in outside,
            "command": defn.command,
            "requires": list(defn.requires),
        }
    return visibility


def _assemble_all_dalton_skills_fulltext() -> str:
    """Assemble full text of all 51 Dalton skills from repo dirs only (never ~/.ptc-agent)."""
    disk_skills = sorted(
        p.name for p in DALTON_SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    assert len(disk_skills) == 51
    repo_skill_dirs = [str(DALTON_SKILLS_DIR)]

    sections: list[str] = []
    for name in disk_skills:
        skill_dir = resolve_skill_dir(name, skill_dirs=repo_skill_dirs)
        assert skill_dir is not None, f"Could not resolve skill dir for {name}"
        assert skill_dir.resolve().is_relative_to(DALTON_SKILLS_DIR.resolve())
        skill_md = load_skill_content(name, skill_dirs=repo_skill_dirs)
        assert skill_md is not None, f"Could not load SKILL.md for {name}"

        parts = [f"===== SKILL: {name} =====", f"--- FILE: {name}/SKILL.md ---", skill_md]
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            for ref_file in sorted(p for p in refs_dir.rglob("*") if p.is_file()):
                rel = ref_file.relative_to(skill_dir)
                if _has_dot_part(rel):
                    continue
                if ref_file.suffix.lower() not in _SKILL_REFERENCE_EXTENSIONS:
                    continue
                parts.append(f"--- FILE: {name}/{rel.as_posix()} ---")
                parts.append(ref_file.read_text(encoding="utf-8"))
        sections.append("\n".join(parts))
    return "\n\n".join(sections) + "\n"


def _assemble_all_workflows_fulltext() -> str:
    """Assemble full text of all repository-shipped ``workflows/**`` (.js / .json only)."""
    registry = PrebuiltWorkflowRegistry(root=REPO_ROOT)
    workflow_names = registry.names()
    assert len(workflow_names) == 6, f"Expected 6 prebuilt workflows, got {workflow_names}"

    workflows_dir = REPO_ROOT / "workflows"
    sections: list[str] = []
    for name in workflow_names:
        wf_dir = workflows_dir / name
        script_text = registry.get(name)
        assert script_text is not None
        parts = [f"===== WORKFLOW: {name} ====="]
        for path in sorted(p for p in wf_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(workflows_dir)
            if _has_dot_part(rel):
                continue
            if path.suffix.lower() not in _WORKFLOW_EXTENSIONS:
                continue
            text = script_text if path.name == "workflow.js" else path.read_text(encoding="utf-8")
            parts.append(f"--- FILE: {rel.as_posix()} ---")
            parts.append(text)
        sections.append("\n".join(parts))
    return "\n\n".join(sections) + "\n"


def _collect_mcp_servers(config: AgentConfig) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    for srv in config.mcp.servers:
        dump = srv.model_dump(mode="json")
        servers.append(
            {
                "name": dump["name"],
                "enabled": dump["enabled"],
                "transport": dump["transport"],
                "tool_exposure_mode": dump["tool_exposure_mode"],
                "call_timeout_s": dump.get("call_timeout_s"),
                "source": dump.get("source"),
                "command": _normalize_repo_path(dump.get("command")),
                "args": _normalize_repo_path(dump.get("args")),
                "url": dump.get("url"),
                "env_keys": sorted((dump.get("env") or {}).keys()),
                "header_keys": sorted((dump.get("headers") or {}).keys()),
            }
        )
    return servers


def _collect_infra_switches() -> dict[str, Any]:
    infra = load_infrastructure_config(str(REPO_ROOT / "config.yaml"))
    return {
        "market_data_providers": [p.model_dump(mode="json") for p in infra.market_data.providers],
        "news_data_providers": [p.model_dump(mode="json") for p in infra.news_data.providers],
        "features": {k: v.model_dump(mode="json") for k, v in sorted(infra.features.items())},
        "mcp_descriptions_enabled": infra.mcp_descriptions.enabled,
        "workflow_child_timeout": infra.workflow.child_timeout,
    }


def _collect_agent_config_switches(config: AgentConfig) -> dict[str, Any]:
    return {
        "llm": {
            "name": config.llm.name,
            "background": config.llm.background,
            "compaction": config.llm.compaction,
            "fetch": config.llm.fetch,
            "fallback": list(config.llm.fallback),
        },
        "search_api": config.search_api,
        "x_search_enabled": bool(config.x_search_enabled),
        "fetch_chain": get_fetch_chain(),
        "sandbox_provider": config.sandbox.provider,
        "sandbox_offered_providers": list(config.sandbox.offered_providers),
        "subagents_enabled": list(config.subagents.enabled),
        "subagents_definition_models": {
            name: defn.model
            for name, defn in sorted(config.subagents.definitions.items())
            if defn.model is not None
        },
    }


async def run_capture(app_env: str) -> dict[str, Any]:
    clear_config_cache()
    get_dalton_config.cache_clear()
    get_web_providers.cache_clear()
    set_turn_data_map(None)
    reset_loader()
    init_loader(session_start_time=FIXED_UTC)

    # Pin data-map token estimation to the deterministic offline fallback formula
    # (builder.py:62) so snapshots never depend on local tiktoken vocab caches.
    orig_estimate = data_map_builder.estimate_data_map_tokens
    data_map_builder.estimate_data_map_tokens = create_autospec(  # type: ignore[assignment]
        orig_estimate,
        side_effect=_offline_token_estimate,
    )
    try:
        raw = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
        config = load_from_dict(raw)
        config.config_file_dir = REPO_ROOT

        cached_data_map_md = _build_fixed_cached_data_map()
        analyst_surface = await _capture_production_graph_surface(
            config, role="analyst", cached_data_map_md=cached_data_map_md
        )
        cos_surface = await _capture_production_graph_surface(
            config, role="chief_of_staff", cached_data_map_md=cached_data_map_md
        )
        insight_surface = _capture_insight_surface(config)

        # Capture both cached-path (filtered through get_or_build_data_map_markdown)
        # and static-fallback data maps for team memory on/off.
        orig_get_cached = db_data_map.get_cached_data_map
        db_data_map.get_cached_data_map = create_autospec(  # type: ignore[assignment]
            orig_get_cached,
            return_value={"rendered_markdown": cached_data_map_md},
        )
        try:
            cached_dm_on = await get_or_build_data_map_markdown(
                search_api=config.search_api,
                x_search_enabled=bool(config.x_search_enabled),
                team_memory_enabled=True,
            )
            cached_dm_off = await get_or_build_data_map_markdown(
                search_api=config.search_api,
                x_search_enabled=bool(config.x_search_enabled),
                team_memory_enabled=False,
            )
        finally:
            db_data_map.get_cached_data_map = orig_get_cached

        static_dm_on = render_static_data_map_markdown(
            search_api=config.search_api,
            x_search_enabled=bool(config.x_search_enabled),
            team_memory_enabled=True,
        )
        static_dm_off = render_static_data_map_markdown(
            search_api=config.search_api,
            x_search_enabled=bool(config.x_search_enabled),
            team_memory_enabled=False,
        )

        dalton_cfg_dump = get_dalton_config().model_dump(mode="json")
        # Defensively pin default pm_user_ids to canonical local-dev-user.
        dalton_cfg_dump["pm_user_ids"] = ["local-dev-user"]

        manifest = {
            "app_env": app_env,
            "main_tools_analyst": analyst_surface["main_tools"],
            "main_tools_chief_of_staff": cos_surface["main_tools"],
            "subagent_tools_analyst": analyst_surface["subagent_tools"],
            "subagent_tools_chief_of_staff": cos_surface["subagent_tools"],
            "insight_surface": insight_surface,
            "dalton_skills_visibility": _collect_dalton_skills_visibility(),
            "mcp_servers": _collect_mcp_servers(config),
            "dalton_config": dalton_cfg_dump,
            "infra_switches": _collect_infra_switches(),
            "agent_config_switches": _collect_agent_config_switches(config),
        }

        return {
            "manifest": manifest,
            "system_prompt_analyst": analyst_surface["main_prompt"],
            "system_prompt_chief_of_staff": cos_surface["main_prompt"],
            "baseline_mcp_servers_analyst": analyst_surface["baseline_mcp_servers"],
            "baseline_mcp_servers_chief_of_staff": cos_surface["baseline_mcp_servers"],
            "subagent_prompts_analyst": analyst_surface["subagent_prompts"],
            "subagent_prompts_chief_of_staff": cos_surface["subagent_prompts"],
            "data_map_cached_team_memory_on": cached_dm_on,
            "data_map_cached_team_memory_off": cached_dm_off,
            "data_map_team_memory_on": static_dm_on,
            "data_map_team_memory_off": static_dm_off,
            "skills_51_assembled": _assemble_all_dalton_skills_fulltext(),
            "workflows_6_fulltext": _assemble_all_workflows_fulltext(),
        }
    finally:
        data_map_builder.estimate_data_map_tokens = orig_estimate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-env", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    assert os.environ.get("APP_ENV") == args.app_env
    result = asyncio.run(run_capture(args.app_env))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
