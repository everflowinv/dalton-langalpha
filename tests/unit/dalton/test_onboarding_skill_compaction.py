"""Skills across a summary: the reload contract for a sandbox-carried skill,
and the continuation contract for the hidden ``dalton-onboarding``.

A summary that replaces a skill's body tells the agent to Read its
``.agents/skills/<name>/SKILL.md`` back — the verifiable continuation for any
skill the sandbox carries. ``dalton-onboarding`` is hidden (host-injected
only, ``dalton_registry.py``), so the sandbox has no file to read back and
``compacted_skills`` leaves it out: the summary never sends the agent to a
file that is not there. What the hidden skill has instead is re-activation:
the onboarding UI (``/welcome``, and Settings' edit link) re-sends the turn's
``additional_context`` naming the skill, and on that turn the injection
re-pastes the whole SKILL.md body — a body that survives only inside a
summary is not live in the window (``compute_already_loaded`` drops it), so
the re-kickoff brings the protocol back verbatim, no sandbox file needed.
Only while the body is still live in the window does a re-activation skip
re-pasting it and refresh the instruction alone.

Scope: these are unit tests over the real registry, the real marker scanner,
the real ``compute_already_loaded``/``_inject_requested_skills`` path and the
host's actual ``plugins/dalton/skills/dalton-onboarding/SKILL.md``; they do
not run a model or a graph turn.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ptc_agent.agent.middleware.compaction.utils import (
    compacted_skills,
    skill_reload_note,
)
from ptc_agent.agent.middleware.skills.content import (
    load_skill_content,
    skill_bodies,
)
from ptc_agent.agent.middleware.skills.middleware import SkillsMiddleware
from ptc_agent.agent.middleware.skills.registry import get_skill, skills_outside_sandbox

#: The instruction the onboarding kickoff rides (`daltonInterviewKickoff`,
#: web/src/pages/Onboarding/dalton/interview.tsx).
KICKOFF_INSTRUCTION = (
    "Start the Dalton onboarding interview: ask which stocks or assets the user holds "
    "or watches and which markets they follow, then collect investment style, risk "
    "tolerance, and answer preferences before marking onboarding complete."
)


def _kickoff(name: str, mid: str) -> HumanMessage:
    """The user message the host inlined a skill body into, in the marker
    format production emits and scans: double-quoted attributes, ``mid``
    bound to the message's own id."""
    return HumanMessage(
        f"run it\n<loaded-skill name=\"{name}\" mid=\"{mid}\">body of {name}</loaded-skill>",
        id=mid,
    )


def _read_skill(call_id: str, name: str) -> list:
    """A whole Read of a skill's SKILL.md, as the reload note sends the
    agent to do."""
    return [
        AIMessage(
            "",
            id=f"ai-{call_id}",
            tool_calls=[
                {"name": "Read", "id": call_id, "args": {"file_path": f".agents/skills/{name}/SKILL.md"}}
            ],
        ),
        ToolMessage(f"# {name}\n\nthe procedure", tool_call_id=call_id, id=f"tm-{call_id}"),
    ]


def test_a_normal_skill_is_handed_back_to_be_read_again():
    """A sandbox-carried skill whose body a summary replaced is listed, and
    the note names the file to Read back."""
    summarized = [*_read_skill("r1", "dcf-model")]

    (name,) = compacted_skills(summarized, [])
    assert name == "dcf-model"
    assert skill_reload_note([name]) == (
        "\n\nThe instructions of these skills were in the summarized part and "
        "are no longer in your context: `dcf-model`. Before you continue with one "
        "you are still following, Read its `.agents/skills/<name>/SKILL.md` again."
    )


def test_dalton_onboarding_is_not_sent_to_a_file_it_does_not_have():
    """Hidden means host-injected only: the sandbox never carried
    ``dalton-onboarding``/SKILL.md, so a summary that replaces its body must
    not list it for re-reading, while a carried skill beside it still is.

    The body's presence is asserted first through the same scanner the
    exclusion reads (``skill_bodies``): only a marker the production regex
    recognizes can put the skill on the summarized side at all, so this test
    cannot pass through a marker format drift."""
    skill = get_skill("dalton-onboarding")
    assert skill is not None and skill.hidden
    assert "dalton-onboarding" in skills_outside_sandbox()

    injected = _kickoff("dalton-onboarding", "u1")
    assert skill_bodies([injected]) == {"dalton-onboarding": True}

    summarized = [injected, *_read_skill("r1", "dcf-model")]

    assert compacted_skills(summarized, []) == ["dcf-model"]
    note = skill_reload_note(compacted_skills(summarized, []))
    assert "dalton-onboarding" not in note
    assert "dcf-model" in note


def _thread_after_a_summary() -> dict:
    """The thread state a re-kickoff lands on: the kickoff message carrying
    the skill body is still in the checkpoint, but a compaction event stands
    in front of it, so the effective window holds the summary and the tail
    only — the body is gone verbatim from what the model sees."""
    kickoff = _kickoff("dalton-onboarding", "u1")
    summary = HumanMessage("<context_summary>The interview asked for holdings.</context_summary>", id="s1")
    tail = [
        AIMessage("Which markets do you follow?", id="a1"),
        HumanMessage("US large caps, and AAPL.", id="u2"),
    ]
    messages = [kickoff, *tail]
    return {
        "state": {
            "messages": messages,
            "loaded_skills": ["dalton-onboarding"],
            "_summarization_event": {
                "cutoff_index": 1,
                "anchor_message_id": "a1",
                "summary_message": summary,
                "file_path": None,
            },
        },
        "tail": tail,
        "kickoff": kickoff,
    }


def _re_kickoff(state: dict) -> dict | None:
    """The update the skills middleware returns for the re-kickoff turn, on
    the real path: ``skill_contexts`` from the request config, dedup against
    what is live through ``compute_already_loaded``."""
    middleware = SkillsMiddleware()
    config = {
        "configurable": {
            "skill_contexts": [
                {"name": "dalton-onboarding", "instruction": KICKOFF_INSTRUCTION}
            ]
        }
    }
    return middleware._inject_requested_skills(state, config)


@pytest.mark.asyncio
async def test_the_re_kickoff_after_the_summary_re_pastes_the_whole_body():
    """The verifiable continuation: a body that only a summary still tells of
    is not live in the window, so the re-kickoff turn re-pastes the full
    SKILL.md body from the host's copy beside the fresh instruction — the
    hidden skill needs no sandbox file to come back whole."""
    thread = _thread_after_a_summary()

    update = await _re_kickoff(thread["state"])

    assert update is not None
    assert update["loaded_skills"] == ["dalton-onboarding"]
    body = load_skill_content("dalton-onboarding")
    assert body, "the host's copy of the skill must be readable"
    injected = str(update["messages"][-1].content)
    assert body in injected
    assert KICKOFF_INSTRUCTION in injected
    # Appended to the re-kickoff's own message, not the summarized kickoff.
    assert update["messages"][-1].id == thread["state"]["messages"][-1].id


@pytest.mark.asyncio
async def test_a_live_body_is_not_pasted_twice_only_the_instruction_refreshes():
    """Without a summary the window still holds the kickoff's body, so the
    same re-activation takes the dedup branch: the instruction refreshes and
    the body is not pasted again — the counterfactual that pins the branch
    the previous test exercises."""
    kickoff = _kickoff("dalton-onboarding", "u1")
    state = {
        "messages": [
            kickoff,
            AIMessage("Which markets do you follow?", id="a1"),
            HumanMessage("US large caps.", id="u2"),
        ],
        "loaded_skills": ["dalton-onboarding"],
        "_summarization_event": None,
    }

    update = await _re_kickoff(state)

    assert update is not None
    assert "loaded_skills" not in update
    injected = str(update["messages"][-1].content)
    assert KICKOFF_INSTRUCTION in injected
    assert "body of dalton-onboarding" not in injected
    assert "<loaded-skill" not in injected


def test_a_fresh_threads_kickoff_gets_the_whole_body_again():
    """Where the thread has never loaded the skill, the same kickoff inlines
    the full protocol from the host's copy."""
    from ptc_agent.agent.middleware.skills.content import SkillRequest, build_skill_content

    fresh = build_skill_content(
        [SkillRequest(name="dalton-onboarding", instruction=KICKOFF_INSTRUCTION)]
    )

    assert fresh is not None and fresh.loaded_skill_names == ["dalton-onboarding"]
    body = load_skill_content("dalton-onboarding")
    assert body in fresh.content
    assert KICKOFF_INSTRUCTION in fresh.content
