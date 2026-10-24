"""DALTON-HOOK(html-md-canonical): with output_format=html, Dalton skill
deliverables stay Markdown and HTML is made only at formal publication."""

import pytest

from ptc_agent.agent.prompts import get_loader, reset_loader

NARROWING = "This preference is narrowed for Dalton work"


def _render(agent_preference, main_agent_steering=True):
    reset_loader()
    profile = {"name": "Demo", "timezone": "UTC", "locale": "en-US"}
    if agent_preference is not None:
        profile["agent_preference"] = agent_preference
    return get_loader().render(
        "components/user_profile.md.j2",
        user_profile=profile,
        user_data_counts=None,
        main_agent_steering=main_agent_steering,
    )


@pytest.fixture(autouse=True)
def _reset_loader():
    reset_loader()
    yield
    reset_loader()


def test_html_preference_gets_dalton_narrowing():
    out = _render({"output_format": "html"})
    assert NARROWING in out
    assert out.index("Output Format: Styled HTML") < out.index(NARROWING)
    assert "only at formal publication" in out
    assert "`publish_report`" in out
    assert "Do not pass it to `publish_report`" in out
    assert "where no skill or workflow names an output file, may still go straight to HTML" in out
    assert "`drafts/foo.md` → `drafts/foo.html`" in out


@pytest.mark.parametrize(
    "pref,steer",
    [({"output_format": "markdown"}, True), ({"output_format": "html"}, False), (None, True)],
)
def test_no_narrowing_without_html_main_agent(pref, steer):
    assert NARROWING not in _render(pref, main_agent_steering=steer)
