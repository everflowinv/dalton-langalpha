"""A LangSmith tracer that does not hold a run's tree.

When tracing is on, langchain_core builds a ``LangChainTracer`` for every run
that names none, and that tracer appends each run to its parent, so a root
holds its whole tree until it ends: every node's inputs and every model call's
serialized messages. A turn has no step cap, so that tree grows for as long as
the turn runs. Naming this tracer in a run's callbacks stops langchain_core
from building its own, and the runs still land where they did.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tracers.langchain import LangChainTracer
from langchain_core.tracers.schemas import Run

logger = logging.getLogger(__name__)


class NonRetainingTracer(LangChainTracer):
    @staticmethod
    def _add_child_run(parent_run: Run, child_run: Run) -> None:
        # Posting never reads child_runs (each run is sent on its own, with
        # its parent id and dotted order), and a turn has no step cap, so
        # keeping them would grow the root without bound.
        return None


def with_run_tracer(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` with a ``NonRetainingTracer`` added to its callbacks.

    Unchanged when tracing is off, or when the callbacks already carry a
    tracer or are a callback manager, which brings its parent's tracer. The
    tracer is built from the context langchain_core's ``_configure`` reads for
    its own (enabled, project or agent address, client, tags and metadata), so
    it has to be called where the run starts, and once per run, since its
    ``order_map`` lives as long as it does.
    """
    callbacks = config.get("callbacks")
    if callbacks is not None and not isinstance(callbacks, list):
        return config
    if any(isinstance(cb, LangChainTracer) for cb in callbacks or ()):
        return config
    tracer = _ambient_equivalent()
    if tracer is None:
        return config
    return {**config, "callbacks": [*(callbacks or []), tracer]}


def _ambient_equivalent() -> NonRetainingTracer | None:
    try:
        from langchain_core.tracers.context import (
            _get_tracer_kwargs,
            _tracing_v2_is_enabled,
            tracing_v2_callback_var,
        )

        # A tracer set by ``tracing_v2_enabled`` is what _configure would add.
        if not _tracing_v2_is_enabled() or tracing_v2_callback_var.get() is not None:
            return None
        # The same kwargs _configure builds its own tracer from, so the
        # destination (a parent's project and address, else the ambient one)
        # resolves exactly as it would without us.
        return NonRetainingTracer(**_get_tracer_kwargs())
    except Exception as e:
        # langchain_core then builds its own tracer, which still traces the
        # run; it only holds the tree again.
        logger.warning("Falling back to the ambient LangSmith tracer: %r", e)
        return None
