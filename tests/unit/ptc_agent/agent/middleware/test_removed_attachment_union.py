"""``_removed_attachment_ids`` unions same-step writes.

DALTON-HOOK(attachment-native-cap): compaction's fit and the strip's image
byte total can each write the key in one model call, and a key without a
reducer refuses the second write (``InvalidUpdateError``). The union mirrors
``_unsent_thinking_ids``; the compaction writers shrink the set by value
(``state & kept``), and a same-step addition names a message inside the kept
view, so unioning the two written values is exactly the merged semantics.
"""

from __future__ import annotations

from typing import Annotated, NotRequired, get_type_hints

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from typing_extensions import TypedDict

from ptc_agent.agent.middleware.compaction.types import CompactionState
from ptc_agent.agent.middleware.thinking_binding import shrink_ids


def _reducer_of(schema: type, key: str):
    """The channel reducer ``schema`` declares for ``key``, None for none.

    Resolved through ``get_type_hints``: the schemas hold string annotations,
    and the reducer is the last callable in the annotation's metadata, past
    any markers (``PrivateStateAttr``).
    """
    hints = get_type_hints(schema, include_extras=True)
    hint = hints.get(key)
    if hint is None:
        return None
    callables = [m for m in getattr(hint, "__metadata__", ()) if callable(m)]
    return callables[-1] if callables else None


class TestRemovedAttachmentUnion:
    def test_the_key_declares_a_union_reducer(self):
        reducer = _reducer_of(CompactionState, "_removed_attachment_ids")
        assert callable(reducer)
        assert reducer(None, {"a"}) == {"a"}
        assert reducer({"a", "b"}, {"b", "c"}) == {"a", "b", "c"}

    def test_the_reducer_is_the_one_unsent_thinking_uses(self):
        """Two keys with two middleware writers each; one reducer, not two
        behaviours to keep in line."""
        from ptc_agent.agent.middleware.thinking_binding import UnsentThinkingState, _union

        assert _reducer_of(CompactionState, "_removed_attachment_ids") is _union
        assert _reducer_of(UnsentThinkingState, "_unsent_thinking_ids") is _union


class _BothWriters(TypedDict):
    _removed_attachment_ids: Annotated[NotRequired[set[str]], _reducer_of(CompactionState, "_removed_attachment_ids")]


@pytest.mark.asyncio
async def test_two_writes_in_one_step_union_instead_of_refusing():
    """A step where compaction's fit and the strip each write the key — the
    shape that raised InvalidUpdateError on a plain LastValue channel."""
    graph = StateGraph(_BothWriters)
    graph.add_node(
        "call",
        lambda _state: [
            # Compaction's fit: state's ids plus what this send removed.
            Command(update={"_removed_attachment_ids": {"fit-new"}}),
            # The strip's byte total: state's ids plus the messages it left out.
            Command(update={"_removed_attachment_ids": {"strip-new"}}),
        ],
    )
    graph.add_edge(START, "call")
    graph.add_edge("call", END)
    app = graph.compile()
    state = await app.ainvoke({"_removed_attachment_ids": {"held"}})
    assert state["_removed_attachment_ids"] == {"held", "fit-new", "strip-new"}


@pytest.mark.asyncio
async def test_the_summary_shrink_drops_retired_ids_under_the_union():
    """``shrink_ids`` produces a set equal to the kept subset whose ``.dropped``
    ids are removed by ``_union`` when applied to the channel."""
    from ptc_agent.agent.middleware.compaction.offloading import removed_attachments

    state_ids = {"gone-with-the-summary", "kept-1"}
    reducer = _reducer_of(CompactionState, "_removed_attachment_ids")
    assert removed_attachments({"_removed_attachment_ids": state_ids}) == state_ids
    shrunk = shrink_ids(state_ids, {"kept-1"})
    assert shrunk == {"kept-1"}
    assert reducer(state_ids, shrunk) == {"kept-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("shrink_first", [True, False])
async def test_shrink_and_same_step_addition_both_take_effect(shrink_first):
    """A summary or window trim's ``shrink_ids`` write drops retired ids while
    preserving any concurrent same-step addition from the strip in either order."""
    cmds = [
        Command(update={"_removed_attachment_ids": shrink_ids({"gone", "kept-1"}, {"kept-1"})}),
        Command(update={"_removed_attachment_ids": {"kept-2"}}),
    ]
    if not shrink_first:
        cmds = list(reversed(cmds))
    graph = StateGraph(_BothWriters)
    graph.add_node("trim", lambda _state: cmds)
    graph.add_edge(START, "trim")
    graph.add_edge("trim", END)
    app = graph.compile()
    state = await app.ainvoke({"_removed_attachment_ids": {"gone", "kept-1"}})
    assert state["_removed_attachment_ids"] == {"kept-1", "kept-2"}
