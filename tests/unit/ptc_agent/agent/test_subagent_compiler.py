"""Tests for SubagentCompiler credential injection (Task 7).

Verifies that a resolved BaseChatModel client from AgentConfig.subsidiary_llm_clients
reaches the compiled SubAgent dict that subagent.py:432 consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

# DALTON-HOOK(user-visible-text): builtins imported for the rule-reaches-subagents tests
from ptc_agent.agent.subagents.builtins import BUILTIN_SUBAGENTS
from ptc_agent.agent.subagents.compiler import SubagentCompiler
from ptc_agent.agent.subagents.definition import SubagentDefinition
from ptc_agent.config import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


@dataclass
class _FakeClient:
    """Minimal BaseChatModel stand-in with ``.model_copy()``."""

    name: str
    _tag: object = field(default_factory=object)

    def model_copy(self) -> "_FakeClient":
        return replace(self, _tag=object())


def _config(**overrides) -> AgentConfig:
    base = dict(
        llm=LLMConfig(name="main-placeholder"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    base.update(overrides)
    return AgentConfig(**base)


def _defn(name: str = "research", model: str | None = "cheap-placeholder") -> SubagentDefinition:
    return SubagentDefinition(
        name=name,
        description="test subagent",
        custom_prompt="stub prompt",
        model=model,
    )


def _compiler(**kwargs) -> SubagentCompiler:
    """Build a compiler with no sandbox/mcp so compile() never touches FS."""
    return SubagentCompiler(**kwargs)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSubagentCompilerClientInjection:
    def test_instance_injection(self):
        """Resolved client instance replaces the string model name."""
        stored = _FakeClient(name="byok-subagent-model")
        cfg = _config(subsidiary_llm_clients={"subagent:research": stored})
        compiler = _compiler(config=cfg)

        result = compiler.compile(_defn(name="research", model="cheap-placeholder"))

        # model key must be a client instance, not the string
        assert isinstance(result["model"], _FakeClient)
        assert result["model"] is not stored  # model_copy -> distinct object
        assert result["model"].name == stored.name

    def test_string_fallback_when_no_subagent_client(self):
        """Falls back to definition.model string when subsidiary_llm_clients has no entry."""
        cfg = _config(subsidiary_llm_clients={})
        compiler = _compiler(config=cfg)

        result = compiler.compile(_defn(name="research", model="cheap-placeholder"))

        assert result["model"] == "cheap-placeholder"

    def test_back_compat_no_config(self):
        """SubagentCompiler(config=None) behaves exactly as before — string passthrough."""
        compiler = _compiler()  # no config kwarg

        result = compiler.compile(_defn(name="research", model="cheap-placeholder"))

        assert result["model"] == "cheap-placeholder"

    def test_back_compat_no_model_no_config(self):
        """definition.model=None and no config → no 'model' key."""
        compiler = _compiler()

        result = compiler.compile(_defn(name="research", model=None))

        assert "model" not in result

    def test_consumption_shape(self):
        """agent_.get('model', default_model) returns the injected instance (exact consumer expression)."""
        stored = _FakeClient(name="byok-subagent-model")
        cfg = _config(subsidiary_llm_clients={"subagent:worker": stored})
        compiler = _compiler(config=cfg)

        agent_ = compiler.compile(_defn(name="worker", model="cheap-placeholder"))
        default_model = "default-placeholder"

        # This is the exact expression from subagent.py:432
        subagent_model = agent_.get("model", default_model)

        assert isinstance(subagent_model, _FakeClient)
        assert subagent_model.name == stored.name


# ── Prompt feature gating ─────────────────────────────────────────────────────


def _base_path_defn(tools: list[str] | None = None) -> SubagentDefinition:
    """Definition with no custom prompt → base template + tool_guide render."""
    return SubagentDefinition(
        name="research",
        description="test subagent",
        role_prompt="Do research.",
        tools=list(tools) if tools is not None else ["execute_code", "finance", "web_search"],
        brokerages="on",  # DALTON-HOOK(hide-trading): keep unrestricted for bare-compiler tool_guide tests
    )


class TestSubagentPromptTimeRules:
    """A subagent reads its own turn stamp, so its prompt states how to read one."""

    def test_the_prompt_states_the_time_rules(self):
        prompt = _compiler(config=_config()).compile(_base_path_defn())["system_prompt"]
        assert "<time_rules>" in prompt
        assert "Each turn opens with a stamp" in prompt


class TestSubagentPromptFeatureGating:
    """The prompt must mirror the tool binding: a tool_guide tier the subagent
    cannot call would drive calls to a tool that is not bound. watch_market is
    doubly gated — the per-user feature flag (agent.py) on top of the binding."""

    def test_watch_market_absent_when_flag_off(self):
        compiler = _compiler(config=_config(features={"market_watch": False}))
        result = compiler.compile(_base_path_defn())
        assert "watch_market" not in result["system_prompt"]

    def test_watch_market_present_when_flag_on(self):
        compiler = _compiler(config=_config(features={"market_watch": True}))
        result = compiler.compile(_base_path_defn())
        assert "watch_market" in result["system_prompt"]

    def test_watch_market_absent_without_the_finance_binding(self):
        compiler = _compiler(config=_config(features={"market_watch": True}))
        result = compiler.compile(_base_path_defn(tools=["execute_code"]))
        assert "watch_market" not in result["system_prompt"]

    def test_no_config_fails_closed(self):
        compiler = _compiler()
        result = compiler.compile(_base_path_defn())
        assert "watch_market" not in result["system_prompt"]

    def test_tiers_follow_the_tool_sets(self):
        """report-builder's binding: no finance, no web, no widgets."""
        compiler = _compiler(config=_config(features={"market_watch": True}))
        prompt = compiler.compile(
            _base_path_defn(tools=["execute_code", "filesystem", "bash"])
        )["system_prompt"]

        assert "Financial Tools (Direct)" not in prompt
        assert "get_quote" not in prompt
        assert "ShowWidget" not in prompt

    def test_finance_tier_present_when_bound(self):
        compiler = _compiler(config=_config(features={"market_watch": False}))
        prompt = compiler.compile(_base_path_defn(tools=["execute_code", "finance"]))["system_prompt"]

        # DALTON-HOOK(prompt-compaction): the direct tool table was dropped; tool definitions describe them
        assert "Financial Tools (Direct)" in prompt
        assert "Direct question about a stock or company → Financial (Direct)" in prompt


class TestSubagentPreloadedSkills:
    """A preferred skill is a preload and the pane offers it on every
    subagent, so its SKILL.md closes whichever prompt the subagent has."""

    @pytest.mark.parametrize(
        "prompt",
        [
            {"role_prompt": "Do ops."},
            {"custom_prompt": "You are ops."},
            {"custom_prompt_template": "subagents/roles/general.md.j2"},
        ],
        ids=["composed", "custom_prompt", "custom_prompt_template"],
    )
    def test_a_preloaded_skill_closes_the_prompt(self, monkeypatch, prompt):
        monkeypatch.setattr(
            "ptc_agent.agent.subagents.compiler.load_skill_content",
            lambda name, dirs=None, registry=None: f"{name} body",
        )
        defn = SubagentDefinition(name="ops", description="d", preload_skills=["dcf"], **prompt)

        assert _compiler().compile(defn)["system_prompt"].endswith(
            "\n\n<preloaded_skills>\n## Skill: dcf\n\ndcf body\n</preloaded_skills>\n"
        )

    def test_a_custom_prompt_without_preloads_is_kept_as_written(self):
        defn = SubagentDefinition(name="ops", description="d", custom_prompt="You are ops.  ")

        # DALTON-HOOK(subagent-workspace-root): custom_prompt gets the workspace root suffix and no <preloaded_skills>
        prompt = _compiler().compile(defn)["system_prompt"]
        assert prompt.startswith("You are ops.")
        assert "<preloaded_skills>" not in prompt
        assert "Workspace root directory (absolute): `/home/workspace`" in prompt


_USER_VISIBLE = "所有直接面向用户的文字使用用户当前要求的语言"


class TestUserVisibleTextReachesSubagents:
    """The shared rule is on the base template and on prompts that skip it."""

    def test_report_builder_prompt_includes_the_rule_once(self):
        prompt = _compiler(config=_config()).compile(
            BUILTIN_SUBAGENTS["report-builder"]
        )["system_prompt"]
        assert prompt.count(_USER_VISIBLE) == 1

    def test_custom_prompt_includes_the_rule_once(self):
        prompt = _compiler().compile(
            SubagentDefinition(name="ops", description="d", custom_prompt="You are ops.")
        )["system_prompt"]
        assert prompt.count(_USER_VISIBLE) == 1
        assert prompt.startswith("You are ops.")

    def test_heading_only_custom_prompt_or_template_still_gets_the_full_rule_once(
        self, monkeypatch
    ):
        """A heading with the same name is not the rule. Both custom paths
        still receive the rendered component exactly once."""
        from ptc_agent.agent.prompts import get_loader
        from ptc_agent.agent.prompts.loader import PromptLoader

        rule = get_loader().get_component("user_visible_text").strip()
        original = PromptLoader.render

        def render(self, template_name, **kwargs):
            if template_name == "heading_only.md.j2":
                return "## 用户可见文字"
            return original(self, template_name, **kwargs)

        monkeypatch.setattr(PromptLoader, "render", render)
        cases = [
            SubagentDefinition(
                name="ops", description="d", custom_prompt="## 用户可见文字"
            ),
            SubagentDefinition(
                name="ops",
                description="d",
                custom_prompt_template="heading_only.md.j2",
            ),
        ]
        for defn in cases:
            prompt = _compiler().compile(defn)["system_prompt"]
            assert prompt.count(rule) == 1
            assert prompt.count("## 用户可见文字") == 2
            assert prompt.startswith("## 用户可见文字")


_DENSE_IMAGE = "不得修改转录值来消除差额"


class TestDenseImageReadingReachesSubagents:
    """DALTON-HOOK(dense-image-reading): one copy on the base, on output
    guidelines, and on prompts that skip the base. A matching heading is not
    the component."""

    def test_report_builder_prompt_includes_the_component_once(self):
        prompt = _compiler(config=_config()).compile(
            BUILTIN_SUBAGENTS["report-builder"]
        )["system_prompt"]
        assert prompt.count(_DENSE_IMAGE) == 1
        assert prompt.count("## 密集图片与表格") == 1

    def test_output_guidelines_section_includes_the_component_once(self):
        defn = SubagentDefinition(
            name="ops",
            description="d",
            role_prompt="Do ops.",
            sections={"output_guidelines": True},
        )
        prompt = _compiler(config=_config()).compile(defn)["system_prompt"]
        assert prompt.count(_DENSE_IMAGE) == 1

    def test_custom_prompt_includes_the_component_once(self):
        prompt = _compiler().compile(
            SubagentDefinition(name="ops", description="d", custom_prompt="You are ops.")
        )["system_prompt"]
        assert prompt.count(_DENSE_IMAGE) == 1
        assert prompt.startswith("You are ops.")

    def test_heading_only_custom_prompt_still_gets_the_full_component_once(self):
        prompt = _compiler().compile(
            SubagentDefinition(
                name="ops", description="d", custom_prompt="## 密集图片与表格"
            )
        )["system_prompt"]
        assert prompt.count(_DENSE_IMAGE) == 1
        assert prompt.count("## 密集图片与表格") == 2
        assert prompt.startswith("## 密集图片与表格")


# DALTON-HOOK(dispatch-delivery): built-in and custom subagent prompts include
# the delivery rule once. A heading of the same name is not the rule.
_DELIVERY = "向父代理（主代理）返回的最终消息须回答派发时提出的问题"


class TestSubagentDeliveryReachesSubagents:
    @pytest.mark.parametrize("name", sorted(BUILTIN_SUBAGENTS))
    def test_builtin_prompt_includes_the_rule_once(self, name):
        prompt = _compiler(config=_config()).compile(BUILTIN_SUBAGENTS[name])[
            "system_prompt"
        ]
        assert prompt.count(_DELIVERY) == 1
        assert prompt.count("## 子代理交付") == 1

    def test_custom_prompt_includes_the_rule_once(self):
        prompt = _compiler().compile(
            SubagentDefinition(name="ops", description="d", custom_prompt="You are ops.")
        )["system_prompt"]
        assert prompt.count(_DELIVERY) == 1
        assert prompt.startswith("You are ops.")

    def test_custom_template_includes_the_rule_once(self):
        prompt = _compiler().compile(
            SubagentDefinition(
                name="ops",
                description="d",
                custom_prompt_template="subagents/roles/general.md.j2",
            )
        )["system_prompt"]
        assert prompt.count(_DELIVERY) == 1

    def test_output_guidelines_on_still_includes_delivery_once(self):
        prompt = _compiler(config=_config()).compile(
            SubagentDefinition(
                name="ops",
                description="d",
                role_prompt="Do the work.",
                tools=["filesystem"],
                sections={"output_guidelines": True},
            )
        )["system_prompt"]
        assert prompt.count(_DELIVERY) == 1
        assert prompt.count("所有直接面向用户的文字使用用户当前要求的语言") == 1

    def test_heading_only_custom_prompt_or_template_still_gets_the_full_rule_once(
        self, monkeypatch
    ):
        from ptc_agent.agent.prompts import get_loader
        from ptc_agent.agent.prompts.loader import PromptLoader

        rule = get_loader().get_component("subagent_delivery").strip()
        original = PromptLoader.render

        def render(self, template_name, **kwargs):
            if template_name == "delivery_heading_only.md.j2":
                return "## 子代理交付"
            return original(self, template_name, **kwargs)

        monkeypatch.setattr(PromptLoader, "render", render)
        cases = [
            SubagentDefinition(
                name="ops", description="d", custom_prompt="## 子代理交付"
            ),
            SubagentDefinition(
                name="ops",
                description="d",
                custom_prompt_template="delivery_heading_only.md.j2",
            ),
        ]
        for defn in cases:
            prompt = _compiler().compile(defn)["system_prompt"]
            assert prompt.count(rule) == 1
            assert prompt.count("## 子代理交付") == 2
            assert prompt.startswith("## 子代理交付")
