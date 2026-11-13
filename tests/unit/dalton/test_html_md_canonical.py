"""DALTON-HOOK(html-md-canonical): Dalton skill deliverables stay Markdown and
HTML is made only at formal publication. The rule lives in the static system
prompt, so an empty user profile still carries it.

DALTON-HOOK(user-visible-text) is in the main agent's static system prompt
(output guidelines), not the profile block: both roles and both guidance
modes receive it, including when the user profile is empty.
"""

import pytest

from ptc_agent.agent.middleware.runtime_context.baseline import (
    BaselineContextMiddleware,
)
from ptc_agent.agent.middleware.runtime_context.profile import render_user_profile
from ptc_agent.agent.prompts import get_loader, reset_loader

USER_VISIBLE = "所有直接面向用户的文字使用用户当前要求的语言"

NARROWING = "This preference is narrowed for Dalton work"

# Same call shape as test_role_prompt: the static system prompt does not read
# the user profile. subagent_summary is rendered with no default.
_SYSTEM = dict(current_time="2026-10-03 11:00 ET", subagent_summary="")


def _render(agent_preference, main_agent_steering=True):
    reset_loader()
    profile = {"name": "Demo", "timezone": "UTC", "locale": "en-US"}
    if agent_preference is not None:
        profile["agent_preference"] = agent_preference
    # Render through the real shaper (agent_preference splits into printed
    # lines and agent_steering upstream of the template), not by hand-rolling
    # the template's variables.
    return render_user_profile(profile, main_agent_steering=main_agent_steering)


@pytest.fixture(autouse=True)
def _reset_loader():
    reset_loader()
    yield
    reset_loader()


@pytest.mark.parametrize(
    "pref",
    [
        {"output_format": ["visual", "markdown", "text"]},
        {"output_format": ["visual", "text", "markdown"]},
        {"output_format": ["visual"]},
        {"output_format": "html"},
        {"deliverable": "visual"},
    ],
)
def test_visual_first_preference_gets_dalton_narrowing(pref):
    """The ranking stays in the profile. The narrowing is in the static prompt."""
    profile = _render(pref)
    assert "1. A visual answer." in profile
    assert NARROWING not in profile
    text = get_loader().get_system_prompt(**_SYSTEM, role="analyst", guidance="detailed")
    assert NARROWING in text
    assert "only at formal publication" in text
    assert "`publish_report`" in text
    assert "Do not pass it to `publish_report`" in text
    assert "where no skill or workflow names an output file, may still use a visual answer" in text
    assert "`drafts/foo.md` → `drafts/foo.html`" in text


@pytest.mark.parametrize(
    "pref",
    [
        {"output_format": ["markdown", "visual", "text"]},
        {"output_format": ["text", "visual", "markdown"]},
        {"output_format": "markdown"},
        {"deliverable": "text"},
        None,
    ],
)
def test_main_agent_narrows_dalton_deliverables_without_visual_first(pref):
    """The Markdown rule applies to every main-agent task, including no ranking."""
    assert NARROWING not in _render(pref, main_agent_steering=True)
    text = get_loader().get_system_prompt(**_SYSTEM, role="analyst", guidance="lean")
    assert NARROWING in text
    assert "按照任务指定的格式制作交付物" in text
    assert "only at formal publication" in text
    assert "`publish_report`" in text


@pytest.mark.parametrize(
    "pref",
    [
        {"output_format": ["visual", "markdown", "text"]},
        {"output_format": ["markdown", "visual", "text"]},
        {"output_format": "html"},
        None,
    ],
)
def test_subagent_does_not_get_the_dalton_narrowing(pref):
    assert NARROWING not in _render(pref, main_agent_steering=False)
    base = get_loader().get_subagent_base_prompt(identity_line="You research.", role_prompt="Do the task.")
    assert NARROWING not in base


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
@pytest.mark.parametrize("guidance", ["lean", "detailed"])
def test_user_visible_text_rule_reaches_every_role_and_mode(role, guidance):
    """The rule is in the static system prompt, outside guidance, role, and
    feature fences, so both roles and both modes see it with no profile."""
    text = get_loader().get_system_prompt(**_SYSTEM, role=role, guidance=guidance)
    assert USER_VISIBLE in text
    assert "任务卡标题" in text
    assert "系统通知" in text
    assert "不展示内部工具名" in text
    assert "子代理向父代理返回内部结果时" in text
    assert "这些内容为英文时" not in text


_DENSE_IMAGE = "不得修改转录值来消除差额"


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
@pytest.mark.parametrize("guidance", ["lean", "detailed"])
def test_dense_image_reading_reaches_every_role_and_mode(role, guidance):
    """DALTON-HOOK(dense-image-reading): the component sits in output guidelines,
    outside role and guidance fences, so each combination sees it once."""
    text = get_loader().get_system_prompt(**_SYSTEM, role=role, guidance=guidance)
    assert text.count(_DENSE_IMAGE) == 1
    assert text.count("## 密集图片与表格") == 1
    assert "已复读一致" in text
    assert "尚未复核" in text
    assert "无法辨认" in text


def test_user_visible_text_rule_when_user_profile_is_empty():
    """An empty profile omits the profile block; the system prompt still has the rule."""
    block = BaselineContextMiddleware(
        user_profile={}, user_data_counts={}
    )._user_profile_block(None)
    assert block == ""
    text = get_loader().get_system_prompt(**_SYSTEM, role="analyst", guidance="lean")
    assert USER_VISIBLE in text


def test_dalton_markdown_rule_when_user_profile_is_empty():
    """baseline.py returns no profile block when the profile and the counts are empty.
    The Markdown rule is in the static prompt, so it is still there."""
    block = BaselineContextMiddleware(
        user_profile={}, user_data_counts={}
    )._user_profile_block(None)
    assert block == ""
    assert NARROWING not in block
    text = get_loader().get_system_prompt(**_SYSTEM, role="analyst", guidance="lean")
    assert NARROWING in text
    assert "按照任务指定的格式制作交付物" in text
    assert "only at formal publication" in text
