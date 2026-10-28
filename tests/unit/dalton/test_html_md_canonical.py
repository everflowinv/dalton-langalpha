"""DALTON-HOOK(html-md-canonical): when visual is ranked first in output_format,
Dalton skill deliverables stay Markdown and HTML is made only at formal publication."""

import pytest

from ptc_agent.agent.middleware.runtime_context.profile import render_user_profile
from ptc_agent.agent.prompts import reset_loader

NARROWING = "This preference is narrowed for Dalton work"


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
