"""Contract for the lean/detailed prompt guidance mechanism.

Locks the resolution order and the invariants that fail silently: an unknown
model must get more scaffolding rather than less, and every template fence must
name a level that exists.

The chain has two owners on purpose. ``resolve_prompt_guidance`` answers from
the deployment pin and the model's own declaration, which is all a build that
never reached the server can know. ``guidance_for`` puts the user's choice on
top of it, and is the only reader of stored preferences.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ptc_agent.agent.middleware.runtime_context.profile import render_user_profile
from ptc_agent.agent.prompts import (
    guidance_template_vars,
    init_loader,
    resolve_prompt_guidance,
)
from ptc_agent.agent.prompts import guidance
from ptc_agent.agent.prompts.guidance import DEFAULT_GUIDANCE, VALID_GUIDANCE
from src.server.services.llm.user_models import guidance_for


def _pinned(level):
    """Pin the deployment-wide level the way ``config.yaml`` does."""
    return patch(
        "src.config.settings.get_prompt_guidance_default", return_value=level
    )


class TestDeclaredOrder:
    """What the model and the deployment say, with no user in the picture."""

    def test_manifest_lean_model(self):
        assert resolve_prompt_guidance("claude-opus-5-5") == "lean"

    def test_deployment_pin_beats_the_manifest(self):
        with _pinned("detailed"):
            assert resolve_prompt_guidance("claude-opus-5-5") == "detailed"

    def test_an_invalid_pin_is_ignored(self):
        with _pinned("yolo"):
            assert resolve_prompt_guidance("claude-opus-5-5") == "lean"

    def test_a_custom_entry_declares_for_itself(self):
        assert resolve_prompt_guidance("my-model", {"prompt_guidance": "lean"}) == "lean"

    def test_a_custom_entry_shadows_the_builtin_it_is_named_after(self):
        """Custom wins by name everywhere else in resolution, so a custom entry
        silent on the level must not inherit the manifest row's."""
        assert resolve_prompt_guidance("claude-opus-5-5", {"model_id": "x"}) == "detailed"


class TestUserPreference:
    """``guidance_for`` is the one reader of the stored bag."""

    def test_empty_bag_falls_through_to_the_manifest(self):
        assert guidance_for({}, "claude-opus-5-5") == "lean"

    def test_user_preference_beats_manifest(self):
        assert guidance_for({"prompt_guidance": "detailed"}, "claude-opus-5-5") == "detailed"

    def test_user_preference_can_opt_a_small_model_into_lean(self):
        assert guidance_for({"prompt_guidance": "lean"}, "gpt-oss-20b") == "lean"

    def test_user_preference_beats_the_deployment_pin(self):
        with _pinned("detailed"):
            assert guidance_for({"prompt_guidance": "lean"}, "gpt-oss-20b") == "lean"

    @pytest.mark.parametrize("value", ["yolo", "", None, True, 1])
    def test_invalid_preference_falls_through_to_manifest(self, value):
        assert guidance_for({"prompt_guidance": value}, "claude-opus-5-5") == "lean"

    def test_a_custom_model_declaration_is_reached_through_the_bag(self):
        """A custom model has no manifest row, so its own entry is the only
        thing standing between it and the fail-safe."""
        bag = {"custom_models": [{"name": "my-model", "prompt_guidance": "lean"}]}
        assert guidance_for(bag, "my-model") == "lean"

    def test_a_malformed_custom_row_does_not_raise(self):
        bag = {"custom_models": ["not-a-dict", {"name": "my-model"}]}
        assert guidance_for(bag, "my-model") == DEFAULT_GUIDANCE


class TestPerModelProfiles:
    """A profile for the running model beats the account-wide value.

    The two are separate storage, so the account-wide setting has to keep
    governing every model the user never tuned.
    """

    @staticmethod
    def _tuned(account, profile, model="claude-opus-5-5"):
        return {
            "prompt_guidance": account,
            "profiles": {model: {"prompt_guidance": profile}},
        }

    def test_profile_beats_the_account_wide_value(self):
        assert guidance_for(self._tuned("lean", "detailed"), "claude-opus-5-5") == "detailed"

    def test_the_profile_governs_only_its_own_model(self):
        assert guidance_for(self._tuned("lean", "detailed"), "gpt-oss-20b") == "lean"

    def test_a_profile_silent_on_guidance_falls_through_to_account_wide(self):
        bag = {
            "prompt_guidance": "detailed",
            "profiles": {"claude-opus-5-5": {"reasoning_effort": "high"}},
        }
        assert guidance_for(bag, "claude-opus-5-5") == "detailed"

    def test_no_model_name_cannot_select_a_profile(self):
        assert guidance_for(self._tuned("detailed", "lean"), None) == "detailed"

    def test_an_invalid_profile_value_discards_the_account_wide_value(self):
        """The user layer answers or abstains as a whole: the value is checked
        after resolution, so a typo in the profile drops the valid account-wide
        setting with it and resolution continues at the manifest.
        """
        assert guidance_for(self._tuned("detailed", "yolo"), "claude-opus-5-5") == "lean"


class TestFailSafeDefault:
    """Unknown or unannotated models must get more guidance, never less."""

    @pytest.mark.parametrize(
        "model_name",
        [None, "", "gpt-oss-20b", "some-byok-model-not-in-manifest"],
    )
    def test_defaults_to_detailed(self, model_name):
        assert resolve_prompt_guidance(model_name) == "detailed"

    def test_intelligence_score_does_not_drive_guidance(self, monkeypatch):
        """`intelligence` is editorial copy for the model picker — a wording
        change there must not move agent behavior. 5 is the score the frontier
        entries were seeded from, so it is the one most likely to leak."""
        from src.llms import LLM

        monkeypatch.setitem(
            LLM.get_model_config().llm_config, "_scored", {"intelligence": 5}
        )
        assert resolve_prompt_guidance("_scored") == "detailed"


class TestTemplateVars:
    def test_detailed_level(self):
        assert guidance_template_vars("detailed") == {"guidance": "detailed"}

    def test_lean_level(self):
        assert guidance_template_vars("lean") == {"guidance": "lean"}


class TestFenceExpressions:
    """Fences compare a level by name, so a typo in the *value* is a silent
    no-op — the block vanishes from both tiers and the subset invariant still
    passes, because lean adds nothing. Nothing else catches this."""

    FENCE = re.compile(r"guidance\s*(?:\|\s*default\(\s*[\"'](\w+)[\"']\s*\)\s*)?==\s*[\"'](\w+)[\"']")

    def _fences(self):
        root = Path(guidance.__file__).parent / "templates"
        for path in sorted(root.rglob("*.j2")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for default, compared in self.FENCE.findall(line):
                    yield path.name, lineno, default, compared

    def test_the_scan_actually_finds_fences(self):
        """Both checks below pass vacuously if the regex matches nothing —
        which is also what a reworded fence syntax would look like."""
        assert list(self._fences()), (
            "no guidance fences found in templates/ — either the fences are "
            "gone or FENCE no longer matches how they are written"
        )

    def test_every_fence_compares_a_real_level(self):
        bad = [
            f"{name}:{lineno} compares guidance to {compared!r}"
            for name, lineno, _, compared in self._fences()
            if compared not in VALID_GUIDANCE
        ]
        assert not bad, "fence compares against a level that does not exist:\n" + "\n".join(bad)

    def test_every_fence_defaults_to_detailed(self):
        """A render path that forgets the key must get more guidance, never
        less — a bare `guidance == "detailed"` silently resolves lean."""
        bad = [
            f"{name}:{lineno} defaults to {default or 'nothing'!r}"
            for name, lineno, default, _ in self._fences()
            if default != DEFAULT_GUIDANCE
        ]
        assert not bad, (
            'every fence needs `| default("detailed")`:\n' + "\n".join(bad)
        )


class TestLeanIsSubsetOfDetailed:
    """One template body, expansions gated — not two copies that can drift."""

    def test_lean_prompt_is_shorter(self):
        loader = init_loader()
        kwargs = dict(subagent_summary="")
        detailed = loader.get_system_prompt(**guidance_template_vars("detailed"), **kwargs)
        lean = loader.get_system_prompt(**guidance_template_vars("lean"), **kwargs)
        assert len(lean) < len(detailed)

    def test_default_render_is_detailed(self):
        """A render that forgets to pass the flag must not silently go lean."""
        loader = init_loader()
        kwargs = dict(subagent_summary="")
        assert loader.get_system_prompt(**kwargs) == loader.get_system_prompt(
            **guidance_template_vars("detailed"), **kwargs
        )


class TestBucketIsInvisibleToThePrompt:
    """Why model-scoped settings never belonged in ``agent_preference``: the
    renderer echoes that bucket verbatim, so a knob stored there would become
    prompt text about itself. Nothing hands the other bucket to this layer any
    more (see ``test_user_profile_cache_key``), and this pins why."""

    def test_model_preference_never_reaches_the_model(self):
        init_loader()
        rendered = render_user_profile(
            {
                "model_preference": {"prompt_guidance": "lean"},
                "agent_preference": {"proactive_questions": "auto"},
            },
            main_agent_steering=True,
        )
        assert "prompt_guidance" not in rendered
        # Ordinary preferences still reach the model.
        assert "proactive_questions" in rendered


# DALTON-HOOK(dispatch-delivery): the dispatch checklist and the source-bearing
# summary render in lean and detailed. Mechanical split rules and the duplicate
# dump example do not.
_DISPATCH_REQUIRED = (
    "派发说明必须包含",
    "核对状态和已知缺失项",
    "本线程任务目录（<task_name>/）",
    "必须生成的文件及完成标准",
    "单次工具调用或简单查询直接做，不派发",
    "同一张截图的转录、复读与差异处理",
    "派发返回的编号只说明任务已建立",
    "打开交付文件检查必要内容",
    "只有成功结束但交付不完整",
    "报告状态并等待指示",
    "用户主动停止的，报告状态并等待指示",
    "按工具给出的恢复或重试说明处理",
    "保持任务未完成",
    "发现未解决差异时",
    "后台任务",
    "不通过循环查询或休眠等待",
    "sleep …; ls …",
    "Launch up to",
)

_DISPATCH_ABSENT = (
    "3+ tool calls",
    "Worked Example",
    "1 sub-agent per element",
    "1 sub-agent per source",
    "18 minutes",
    "Research Python async frameworks",
    "背景任务",
    "end your turn",
    "Do not sleep or poll",
    "Always name this thread's task directory",
    "运行已经结束，但结果为空",
    "Sequence on arrival",
    "Be specific in prompts",
)

_SUMMARY_REQUIRED = (
    "结构化或批量",
    "原始数据路径",
    "覆盖范围",
    "核对状态",
    "未核实项",
    "重要差异",
    "父代理（主代理）",
    "用户可见文字",
    "不展开内部路径",
)

_SUMMARY_ABSENT = (
    "Dump data first",
    "Dump First, Then Process",
    "some_mcp_tool",
    "Return ONLY summary",
    "对话里只放摘要",
    "使主代理能恢复来源",
    "未核实项目",
    "未核实状态",
)

_CHIEF_HANDOFF = (
    "向工作区分析师派发任务",
    "记录并沿用对应的工作区和线程",
    "不再自行做同一套正式计算",
    "明确标为临时估算",
    "跨工作区或外部渠道写入要求",
    "用户最新变更",
    "确认文件存在、可读且包含要求的内容",
    "读取线程结果（`agent_output`）",
    "而不是线程原文",
)


class TestDispatchAndDeliveryPrompt:
    @pytest.mark.parametrize("guidance", ["lean", "detailed"])
    @pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
    def test_dispatch_checklist_and_summary_render_in_both_modes(self, guidance, role):
        prompt = init_loader().get_system_prompt(
            subagent_summary="",
            role=role,
            **guidance_template_vars(guidance),
        )
        for phrase in _DISPATCH_REQUIRED:
            assert phrase in prompt, phrase
        for phrase in _DISPATCH_ABSENT:
            assert phrase not in prompt, phrase
        for phrase in _SUMMARY_REQUIRED:
            assert phrase in prompt, phrase
        for phrase in _SUMMARY_ABSENT:
            assert phrase not in prompt, phrase
        for phrase in _CHIEF_HANDOFF:
            if role == "chief_of_staff":
                assert phrase in prompt, phrase
            else:
                assert phrase not in prompt, phrase

    def test_update_and_resume_example_stays_detailed_only(self):
        loader = init_loader()
        kwargs = dict(subagent_summary="")
        detailed = loader.get_system_prompt(**guidance_template_vars("detailed"), **kwargs)
        lean = loader.get_system_prompt(**guidance_template_vars("lean"), **kwargs)
        assert "Add regulatory analysis" in detailed
        assert "Add regulatory analysis" not in lean
