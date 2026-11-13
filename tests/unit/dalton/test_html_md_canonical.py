"""DALTON-HOOK(html-md-canonical): when visual is ranked first in output_format,
Dalton skill deliverables stay Markdown and HTML is made only at formal publication.

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
    out = _render(pref)
    assert NARROWING in out
    assert out.index("1. A visual answer.") < out.index(NARROWING)
    assert "only at formal publication" in out
    assert "`publish_report`" in out
    assert "Do not pass it to `publish_report`" in out
    assert "where no skill or workflow names an output file, may still use a visual answer" in out
    assert "`drafts/foo.md` → `drafts/foo.html`" in out


@pytest.mark.parametrize(
    "pref,steer",
    [
        ({"output_format": ["markdown", "visual", "text"]}, True),
        ({"output_format": ["text", "visual", "markdown"]}, True),
        ({"output_format": "markdown"}, True),
        ({"deliverable": "text"}, True),
        ({"output_format": ["visual", "markdown", "text"]}, False),
        ({"output_format": "html"}, False),
        (None, True),
    ],
)
def test_no_narrowing_without_visual_first_main_agent(pref, steer):
    assert NARROWING not in _render(pref, main_agent_steering=steer)


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


def test_user_visible_text_rule_when_user_profile_is_empty():
    """An empty profile omits the profile block; the system prompt still has the rule."""
    block = BaselineContextMiddleware(
        user_profile={}, user_data_counts={}
    )._user_profile_block(None)
    assert block == ""
    text = get_loader().get_system_prompt(**_SYSTEM, role="analyst", guidance="lean")
    assert USER_VISIBLE in text
