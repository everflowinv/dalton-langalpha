"""How the structured agent preferences reach the prompt.

``research_depth``, ``deliverable`` and ``proactivity`` each select guidance
in ``components/user_profile.md.j2`` instead of printing as a ``- **key**:``
line, and only in the main agent's copy: a subagent hands its result back to
the main agent, which decides how it reaches the user. The free-form keys
beside them print as before, and a value outside the vocabulary prints like
one of them.
"""

import re

import pytest

from ptc_agent.agent.middleware.runtime_context.baseline import (
    BaselineContextMiddleware,
)
from ptc_agent.agent.middleware.runtime_context.profile import render_user_profile
from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY
from ptc_agent.agent.prompts import reset_loader
from src.server.models.profile_vocabulary import (
    STRUCTURED_PREFERENCES,
    structured_preferences,
)

LEAD = "You MUST follow these user preferences when responding:"
HTML_BLOCK_MARKER = "### Output Format: Styled HTML"
TEXT = "The user's usual answer is text in the chat"
VISUAL = "The user wants answers they can explore"


def _render(agent_preference, main_agent_steering=True, **kwargs):
    reset_loader()
    profile = {"name": "Demo", "timezone": "UTC", "locale": "en-US"}
    if agent_preference is not None:
        profile["agent_preference"] = agent_preference
    profile.update(kwargs.pop("profile", {}))
    return render_user_profile(
        profile, main_agent_steering=main_agent_steering, **kwargs
    )


def _section(out):
    """The Agent Preferences section, or "" when it is not rendered."""
    if "## Agent Preferences" not in out:
        return ""
    rest = out.split("## Agent Preferences", 1)[1]
    return re.split(r"^## ", rest, maxsplit=1, flags=re.MULTILINE)[0]


@pytest.fixture(autouse=True)
def _reset_loader():
    reset_loader()
    yield
    reset_loader()


class TestEachValueSelectsGuidance:
    @pytest.mark.parametrize(
        "key, value, marker",
        [
            ("research_depth", "light", "By default the user wants a light answer"),
            (
                "research_depth",
                "adaptive",
                "By default the user wants the effort to match the question",
            ),
            (
                "research_depth",
                "thorough",
                "By default the user wants research before every answer",
            ),
            ("deliverable", "text", TEXT),
            ("deliverable", "visual", VISUAL),
            (
                "proactivity",
                "proactive",
                "The user wants to hear from you without having to ask",
            ),
            (
                "proactivity",
                "when_needed",
                "The user wants to hear from you only when something needs their attention",
            ),
            (
                "proactivity",
                "on_request",
                "don't offer automations, briefings or alerts",
            ),
        ],
    )
    def test_a_value_renders_as_its_instruction(self, key, value, marker):
        section = _section(_render({key: value}))
        assert marker in section
        assert f"- **{key}**:" not in section
        # Structured keys alone carry no list of free-form keys to lead.
        assert LEAD not in section

    def test_light_offers_the_research_rather_than_starting_it(self):
        section = _section(_render({"research_depth": "light"}))
        assert (
            "give the quick answer and offer the full research rather than starting it unasked"
            in section
        )
        assert (
            "When they ask outright for that work, such as a valuation or a report, do it."
            in section
        )

    def test_thorough_research_is_for_market_questions(self):
        section = _section(_render({"research_depth": "thorough"}))
        assert (
            "before every answer to a market or investing question, even a short one"
            in section
        )

    def test_a_visual_answer_keeps_quick_answers_as_text(self):
        section = _section(_render({"deliverable": "visual"}))
        assert "interactive chart or dashboard inline with `ShowWidget`" in section
        assert "A quick answer stays text in the chat." in section

    def test_a_visual_answer_defers_to_the_surface(self):
        """A chat app states its own rules for what a reply may carry, and
        one may not show widgets."""
        section = _section(_render({"deliverable": "visual"}))
        assert "where this turn's surface shows widgets" in section

    @pytest.mark.parametrize("value", ["proactive", "when_needed"])
    def test_an_offer_is_made_once_and_not_for_what_exists(self, value):
        section = _section(_render({"proactivity": value}))
        assert "no automation of theirs already covers it" in section
        assert "Offer each one once, not on every answer" in section

    def test_every_skill_it_points_at_ships(self):
        section = _section(_render({"deliverable": "visual"}))
        named = set(re.findall(r"\.agents/(skills/[\w-]+/SKILL\.md)", section))
        assert named == {"skills/inline-widget/SKILL.md"}
        assert named <= {skill.skill_md_path for skill in SKILL_REGISTRY.values()}

    def test_the_template_knows_every_value_the_server_accepts(self):
        """The vocabulary is code and the guidance is template text. A value
        added to one and not the other would print as a raw key, so the two
        are pinned together. ``output_format: "markdown"`` is the one value
        that selects nothing: it is the default, written so Settings can name
        it, and alone it renders no section."""
        for key, vocab in STRUCTURED_PREFERENCES["agent_preference"].items():
            assert not vocab.many, key
            for choice in vocab.choices:
                section = _section(_render({key: choice}))
                assert f"- **{key}**:" not in section, f"{key}={choice}"
                if (key, choice) == ("output_format", "markdown"):
                    assert section == "", f"{key}={choice}"
                    continue
                assert section, f"{key}={choice}"

    def test_a_report_is_output_format_not_a_deliverable(self):
        assert (
            "report"
            not in STRUCTURED_PREFERENCES["agent_preference"]["deliverable"].choices
        )

    @pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
    @pytest.mark.parametrize("output_format", [None, "html"])
    def test_every_combination_keeps_its_paragraphs_apart(self, role, output_format):
        """Each paragraph and heading stands on its own, whatever renders
        before or after it: an inline tag at a line's end would swallow its
        newline and run the next paragraph into it."""
        vocab = STRUCTURED_PREFERENCES["agent_preference"]
        for depth in (None, *vocab["research_depth"].choices):
            for deliverable in (None, *vocab["deliverable"].choices):
                for proactivity in (None, *vocab["proactivity"].choices):
                    prefs = {
                        "tone": "terse",
                        "research_depth": depth,
                        "deliverable": deliverable,
                        "proactivity": proactivity,
                        "output_format": output_format,
                    }
                    lines = _section(_render(prefs, role=role)).splitlines()
                    for before, line in zip(lines, lines[1:]):
                        if line.startswith(
                            ("By default", "The user", "###", "You MUST")
                        ):
                            assert before == "", (
                                f"{prefs}: {line[:40]!r} follows {before[:40]!r}"
                            )

    def test_the_section_is_the_same_at_both_guidance_levels(self):
        """User steering: it renders in the baseline, which takes no fence."""
        profile = {
            "name": "Demo",
            "agent_preference": {
                "research_depth": "thorough",
                "deliverable": "visual",
                "proactivity": "proactive",
            },
        }
        lean, detailed = (
            BaselineContextMiddleware(
                user_profile=profile, guidance=guidance
            )._user_profile_block(None)
            for guidance in ("lean", "detailed")
        )
        assert "By default the user wants research before every answer" in lean
        assert lean == detailed


class TestChiefOfStaff:
    """Home's agent hands research to the analysts, so its depth guidance is
    worded around the hand-off its role section describes."""

    def test_light_offers_the_hand_off_and_makes_it_for_work_asked_for(self):
        section = _section(_render({"research_depth": "light"}, role="chief_of_staff"))
        assert (
            "offer the hand-off to an analyst, or the full research, rather than starting it unasked"
            in section
        )
        assert (
            "such as a valuation or a report, do it or hand it off as `<role>` says."
            in section
        )

    def test_adaptive_sends_an_analyst_s_work_to_the_analyst(self):
        section = _section(
            _render({"research_depth": "adaptive"}, role="chief_of_staff")
        )
        assert (
            "by the analyst who owns it when `<role>` makes it an analyst's work."
            in section
        )

    def test_thorough_hands_off_without_the_offer(self):
        section = _section(
            _render({"research_depth": "thorough"}, role="chief_of_staff")
        )
        assert "goes to the analyst without asking first" in section

    @pytest.mark.parametrize("depth", ["light", "adaptive", "thorough"])
    def test_an_analyst_is_never_told_to_hand_off(self, depth):
        section = _section(_render({"research_depth": depth}, role="analyst"))
        assert "analyst" not in section
        assert "`<role>`" not in section


class TestMainAgentOnly:
    def test_a_subagent_with_only_structured_keys_gets_no_section(self):
        out = _render(
            {
                "research_depth": "light",
                "deliverable": "visual",
                "proactivity": "proactive",
            },
            main_agent_steering=False,
        )
        assert "## Agent Preferences" not in out

    def test_a_subagent_keeps_the_free_form_keys_alone(self):
        section = _section(
            _render(
                {"research_depth": "thorough", "deliverable": "text", "tone": "terse"},
                main_agent_steering=False,
            )
        )
        assert f"{LEAD}\n- **tone**: terse" in section
        assert "By default the user wants" not in section
        assert TEXT not in section
        assert "research_depth" not in section and "deliverable" not in section


class TestBesideOtherKeys:
    def test_free_form_keys_keep_their_list_beside_the_guidance(self):
        section = _section(
            _render(
                {"tone": "terse", "proactivity": "on_request", "include_charts": False}
            )
        )
        assert section.count(LEAD) == 1
        assert "- **tone**: terse" in section
        assert "- **include_charts**: False" in section
        assert "The user wants to hear from you only when they ask" in section
        assert "- **proactivity**:" not in section

    @pytest.mark.parametrize("stale", ["text", "visual"])
    def test_html_is_the_deliverable_whatever_deliverable_holds(self, stale):
        """Settings' switch writes only ``output_format``, so the onboarding
        answer it replaced stays in the row; the report wins, and the stale
        value neither guides nor prints."""
        section = _section(
            _render(
                {
                    "deliverable": stale,
                    "output_format": "html",
                    "research_depth": "thorough",
                }
            )
        )
        assert HTML_BLOCK_MARKER in section
        # Its own paragraph, not run into the depth guidance above it.
        assert f"view.\n\n{HTML_BLOCK_MARKER}" in section
        assert TEXT not in section and VISUAL not in section
        assert "- **deliverable**:" not in section
        assert "By default the user wants research before every answer" in section

    def test_html_beside_only_a_stale_deliverable_is_the_html_section_alone(self):
        section = _section(_render({"deliverable": "text", "output_format": "html"}))
        assert section.strip().startswith(HTML_BLOCK_MARKER)

    def test_html_without_a_deliverable_is_the_section_alone(self):
        section = _section(_render({"output_format": "html"}))
        assert HTML_BLOCK_MARKER in section
        assert LEAD not in section

    def test_markdown_leaves_the_deliverable_guiding(self):
        section = _section(
            _render({"deliverable": "visual", "output_format": "markdown"})
        )
        assert VISUAL in section
        assert HTML_BLOCK_MARKER not in section

    def test_markdown_alone_renders_no_section(self):
        assert "## Agent Preferences" not in _render({"output_format": "markdown"})

    def test_a_subagent_gets_neither_the_deliverable_nor_the_html_section(self):
        out = _render(
            {"deliverable": "visual", "output_format": "html"},
            main_agent_steering=False,
        )
        assert "## Agent Preferences" not in out


class TestValuesOutsideTheVocabulary:
    def test_free_text_written_before_the_vocabulary_prints_as_it_was(self):
        """The check passes a value the row already holds, so one from before
        the key had a vocabulary can still reach the prompt."""
        prefs = {
            "research_depth": "medium",
            "deliverable": "report",
            "proactivity": "Sometimes",
        }
        for main in (True, False):
            section = _section(_render(prefs, main_agent_steering=main))
            assert "- **research_depth**: medium" in section
            assert "- **deliverable**: report" in section
            assert "- **proactivity**: Sometimes" in section
            assert "By default the user wants" not in section

    def test_a_list_prints_joined(self):
        section = _section(_render({"avoid": ["jargon", "emoji"]}))
        assert "- **avoid**: jargon, emoji" in section

    @pytest.mark.parametrize("empty", [[], "", None, {}])
    def test_a_key_holding_nothing_has_no_line(self, empty):
        assert "## Agent Preferences" not in _render(
            {"deliverable": empty, "tone": empty}
        )


class TestInvestmentProfile:
    def test_agent_keys_never_reach_the_investment_profile(self):
        """``structured_preferences`` flattens the investing columns alone, so
        a profile holding only agent keys prints no empty Investment Profile."""
        row = {
            "agent_preference": {
                "research_depth": "light",
                "deliverable": "visual",
                "proactivity": "proactive",
            },
        }
        investment = structured_preferences(row)
        assert investment == {}

        out = _render(
            row["agent_preference"], profile={"investment_profile": investment}
        )
        assert "## Investment Profile" not in out
        assert "## Agent Preferences" in out
