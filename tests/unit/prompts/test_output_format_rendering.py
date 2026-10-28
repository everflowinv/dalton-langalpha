"""Tests for output_format steering in the user_profile prompt component.

Renders ``components/user_profile.md.j2`` and asserts the HTML-steering block
appears iff ``main_agent_steering`` (the main agent's copy; a subagent's
leaves it out) and ``agent_preference.output_format == "html"``, and never
double-renders the ``output_format`` key in the generic preference loop
unless its value is outside the vocabulary.
"""

import pytest

from ptc_agent.agent.middleware.runtime_context.profile import render_user_profile
from ptc_agent.agent.prompts import reset_loader

HTML_BLOCK_MARKER = "Output Format: Styled HTML"
SKILL_REFS = (".agents/skills/html-report/SKILL.md", ".agents/skills/ui-design/SKILL.md")


def _render(agent_preference, main_agent_steering=True):
    """Render the user_profile component with the given agent_preference dict.

    ``main_agent_steering`` is True for the main agent's baseline and False
    for a subagent's prompt. Defaults to True so the main agent is the baseline.
    """
    reset_loader()
    profile = {"name": "Demo", "timezone": "UTC", "locale": "en-US"}
    if agent_preference is not None:
        profile["agent_preference"] = agent_preference
    return render_user_profile(profile, main_agent_steering=main_agent_steering)


@pytest.fixture(autouse=True)
def _reset_loader():
    """Ensure a clean loader singleton around each test."""
    reset_loader()
    yield
    reset_loader()


class TestHtmlSteeringBlock:
    """The dedicated HTML block is gated on output_format == "html"."""

    def test_html_renders_block(self):
        out = _render({"output_format": "html"})
        assert HTML_BLOCK_MARKER in out
        assert "<task>/<name>.html" in out
        for ref in SKILL_REFS:
            assert ref in out

    def test_markdown_renders_no_block(self):
        out = _render({"output_format": "markdown"})
        assert HTML_BLOCK_MARKER not in out
        for ref in SKILL_REFS:
            assert ref not in out

    def test_absent_renders_no_block(self):
        out = _render({"proactive_questions": "sometimes"})
        assert HTML_BLOCK_MARKER not in out

    def test_no_agent_preference_renders_no_block(self):
        out = _render(None)
        assert HTML_BLOCK_MARKER not in out
        assert "## Agent Preferences" not in out

    def test_html_no_block_for_a_subagent(self):
        # A subagent hands its result back rather than authoring the
        # deliverable, so HTML steering must not fire even when the user set
        # output_format=html.
        out = _render({"output_format": "html"}, main_agent_steering=False)
        assert HTML_BLOCK_MARKER not in out
        for ref in SKILL_REFS:
            assert ref not in out

    def test_other_prefs_still_render_for_a_subagent(self):
        # Only the main agent's HTML block is gated; other preferences still
        # apply to a subagent, and output_format never double-renders.
        out = _render(
            {"output_format": "html", "tone": "concise"}, main_agent_steering=False
        )
        assert HTML_BLOCK_MARKER not in out
        assert "- **tone**: concise" in out
        assert "- **output_format**:" not in out


class TestGenericLoop:
    """output_format in its vocabulary is excluded from the generic key/value loop."""

    def test_output_format_not_double_rendered(self):
        out = _render({"output_format": "html", "proactive_questions": "sometimes"})
        # The generic loop must not emit an output_format bullet for any value.
        assert "- **output_format**:" not in out

    def test_other_keys_still_loop(self):
        out = _render(
            {"output_format": "html", "proactive_questions": "sometimes", "tone": "concise"}
        )
        assert "- **proactive_questions**: sometimes" in out
        assert "- **tone**: concise" in out

    def test_other_keys_loop_when_format_markdown(self):
        out = _render({"output_format": "markdown", "tone": "concise"})
        assert "- **tone**: concise" in out
        assert "- **output_format**:" not in out

    def test_no_stray_empty_bullet_when_format_only(self):
        # output_format is the only preference -> loop yields nothing, but the
        # excluded key must not leave a dangling "- " bullet behind.
        out = _render({"output_format": "html"})
        assert HTML_BLOCK_MARKER in out
        prefs_section = out.split("## Agent Preferences", 1)[1].split("## Financial Context", 1)[0]
        for line in prefs_section.splitlines():
            assert line.strip() != "-"
            assert line.strip() != "- **"

    def test_no_lead_line_over_an_empty_list(self):
        # With nothing for the loop to print, its lead-in goes too; and a
        # subagent, which gets no HTML block, gets no section at all.
        assert "You MUST follow" not in _render({"output_format": "html"})
        out = _render({"output_format": "html"}, main_agent_steering=False)
        assert "## Agent Preferences" not in out

    def test_a_format_outside_the_vocabulary_prints_like_any_other_key(self):
        """Only a value that selects a section is kept off the list: anything
        else is the user's own words and reaches the model as they wrote it."""
        out = _render({"output_format": "pdf"})
        assert HTML_BLOCK_MARKER not in out
        assert "- **output_format**: pdf" in out
