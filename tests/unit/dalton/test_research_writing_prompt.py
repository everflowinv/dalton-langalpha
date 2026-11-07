"""Unit tests for PW-1: permanent research judgment and writing rules component."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

import ptc_agent.agent.graph as graph_mod
import src.dalton.access_tiers as dalton_tiers
import src.dalton.data_map as dalton_data_map
import src.dalton.sharing as dalton_sharing
import src.server.database.user_vault_secrets as vault_db
from ptc_agent.agent.agent import PTCAgent
from ptc_agent.agent.graph import build_ptc_graph_with_session
from ptc_agent.agent.middleware.runtime_context.epoch import Workspace
from ptc_agent.agent.prompts.loader import PromptLoader
from ptc_agent.agent.subagents.builtins import BUILTIN_SUBAGENTS
from ptc_agent.agent.subagents.compiler import SubagentCompiler
from ptc_agent.agent.subagents.merge import (
    BaseSubagent,
    add_plugin_bases,
    base_subagents,
    merge_subagents,
    plugin_definition,
)
from ptc_agent.config.agent import AgentConfig, SubagentsConfig
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.session import Session
from src.server.services.plugins.bundled import bundled_agent_plans

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_TITLE = "# 研究判断与写作规则"


def _dalton_subagent_definitions() -> dict[str, object]:
    dalton_cfg = (
        yaml.safe_load((REPO_ROOT / "agent_config.dalton.yaml").read_text(encoding="utf-8"))
        or {}
    )
    sub_cfg = SubagentsConfig(**(dalton_cfg.get("subagents") or {}))
    plugin_bases = [
        (BaseSubagent(f"plugin:{bundle}/{plan.name}", plugin_definition(plan.row)), True)
        for bundle, plan in bundled_agent_plans()
    ]
    bases, offered = add_plugin_bases(
        base_subagents(sub_cfg), sub_cfg.enabled, plugin_bases
    )
    resolved = merge_subagents(bases, offered, ())
    return {**BUILTIN_SUBAGENTS, **resolved.definitions}


@pytest.mark.asyncio
async def test_research_writing_prompt_switch_on_and_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When research_writing_enabled is True, main prompts and writing subagents include the component title while data-prep does not; when False (upstream default), none include it."""
    loader = PromptLoader()

    # 1) Upstream default (switch off): main system prompts do not contain the component
    for role in ("analyst", "chief_of_staff"):
        prompt_off = loader.get_system_prompt(role=role)
        assert COMPONENT_TITLE not in prompt_off
        assert "<research_writing>" not in prompt_off

    # 2) Switch on: main system prompts contain the component and both reconciliations
    for role in ("analyst", "chief_of_staff"):
        prompt_on = loader.get_system_prompt(
            role=role,
            research_writing_enabled=True,
        )
        assert COMPONENT_TITLE in prompt_on
        assert "<research_writing>" in prompt_on
        assert (
            "不是你对上涨或下跌概率的判断"
            in prompt_on
        )
        assert (
            "Dalton 的行动分类（立即行动、等待买点、等待证据、暂不参与）不是仓位比例，可以照常使用。"
            in prompt_on
        )
        assert (
            "`risk-reward.md` 规定必须写三情景的任务"
            in prompt_on
        )
        assert "不用英文直译句式" in prompt_on
        assert "不用奖状式开场" in prompt_on
        assert "来源身份正面写，不追加否定" in prompt_on
        assert "方法约束正面写口径，不写自证句" in prompt_on
        assert "只负责取证、核对或审稿的子代理" in prompt_on
        assert "## Report Structure" not in prompt_on

    # 3) Subagents: check builtins + Dalton subagents from agent_config.dalton.yaml
    fake_config = create_autospec(AgentConfig, instance=True)
    fake_config.prompt_guidance_for_role.return_value = "detailed"
    fake_config.feature_enabled.return_value = False
    fake_config.client_for_role.return_value = None

    all_defs = _dalton_subagent_definitions()

    writing_subagents = (
        "report-builder",
        "research",
        "equity-analyst",
        "general-purpose",
        "dalton-evidence",
        "dalton-reviewer",
    )

    compiler_off = SubagentCompiler(
        config=fake_config,
        research_writing_enabled=False,
    )
    for name in (*writing_subagents, "data-prep"):
        compiled_off = compiler_off.compile(all_defs[name])
        assert COMPONENT_TITLE not in compiled_off["system_prompt"], name
        assert "<research_writing>" not in compiled_off["system_prompt"], name

    compiler_on = SubagentCompiler(
        config=fake_config,
        research_writing_enabled=True,
    )
    for name in writing_subagents:
        compiled_on = compiler_on.compile(all_defs[name])
        assert COMPONENT_TITLE in compiled_on["system_prompt"], name
        assert "<research_writing>" in compiled_on["system_prompt"], name

    # Pure data-fetching/code subagent (data-prep) never includes research_writing
    data_prep_on = compiler_on.compile(all_defs["data-prep"])
    assert COMPONENT_TITLE not in data_prep_on["system_prompt"]
    assert "<research_writing>" not in data_prep_on["system_prompt"]

    # 4) Dalton runtime turn builder passes research_writing_enabled=True to PTCAgent.create_agent
    fake_agent = create_autospec(PTCAgent, instance=True)
    fake_agent.create_agent.return_value = "BUILT_GRAPH"
    monkeypatch.setattr(
        graph_mod,
        "PTCAgent",
        create_autospec(PTCAgent, return_value=fake_agent),
    )
    monkeypatch.setattr(
        graph_mod,
        "fetch_user_data_counts",
        create_autospec(graph_mod.fetch_user_data_counts, return_value=None),
    )
    monkeypatch.setattr(
        graph_mod,
        "_read_workspace_naming",
        create_autospec(graph_mod._read_workspace_naming, return_value=Workspace("NVDA")),
    )
    monkeypatch.setattr(
        graph_mod,
        "_read_harness_blocks",
        create_autospec(graph_mod._read_harness_blocks, return_value=({}, None)),
    )
    monkeypatch.setattr(
        vault_db,
        "get_user_secrets_decrypted",
        create_autospec(vault_db.get_user_secrets_decrypted, return_value={}),
    )
    monkeypatch.setattr(
        dalton_tiers,
        "get_user_tier",
        create_autospec(dalton_tiers.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "resolve_turn_team_memory_enabled",
        create_autospec(dalton_sharing.resolve_turn_team_memory_enabled, return_value=False),
    )
    monkeypatch.setattr(
        dalton_data_map,
        "warm_turn_data_map",
        create_autospec(dalton_data_map.warm_turn_data_map, return_value=""),
    )

    fake_session = create_autospec(Session, instance=True)
    fake_session.sandbox = create_autospec(PTCSandbox, instance=True)
    fake_session.sandbox.vault_secrets = {}
    fake_session.mcp_registry = object()
    fake_session.mcp_tool_summary = "SUMMARY"
    fake_config.search_api = "openclaw"

    await build_ptc_graph_with_session(
        session=fake_session,
        config=fake_config,
        user_id="u-richard",
        project=ProjectContext("11111111-1111-4111-8111-111111111111", "NVDA"),
        role="analyst",
    )
    assert fake_agent.create_agent.call_args.kwargs.get("research_writing_enabled") is True


def test_writing_md_deduplicated_and_retains_dalton_mechanisms() -> None:
    """writing.md points to the system prompt for general rules, drops duplicated sections, and retains Dalton-specific mechanisms."""
    writing_md = (
        REPO_ROOT / "plugins/dalton/skills/dalton-conventions/references/writing.md"
    ).read_text(encoding="utf-8")

    # Pointer to system prompt research writing section is present
    assert "通用的判断与写作规则见系统提示词的研究写作部分" in writing_md

    # Removed duplicate paragraphs from writing.md are absent
    assert "开门见山给判断：报告开篇直接亮明立场" not in writing_md
    assert "这不是公司一手表态 / 不是管理层原话" not in writing_md
    assert '想肯定 B 就直接写 B——不写"不是 A，而是 B"' not in writing_md
    assert "严禁在报告中使用互联网黑话与空洞套话" not in writing_md

    # Dalton-specific mechanisms and restored structural rules remain in writing.md
    assert "「跟踪」节" in writing_md
    assert "准确口径" in writing_md
    assert "正文和推送不出现机器内部标识与报错原文" in writing_md
    assert "## 引用" in writing_md
    assert "一句话里的事实数字一般不超过两个" in writing_md
    assert "缺口只说一次，放对位置" in writing_md
    assert "[假设]" in writing_md
    assert '"相当于" vs "占"' in writing_md
    assert "严禁使用波浪号 `~`" in writing_md
    assert "严禁使用 `$` 符号" in writing_md
    assert "The stock works if" in writing_md
