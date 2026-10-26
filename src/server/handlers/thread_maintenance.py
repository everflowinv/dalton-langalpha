"""Thread maintenance — compact and offload under the exclusive thread guard.

Both mutations resolve the thread's graph and checkpoint state, run the
operation with a cross-worker stop key, and write its result together with
the ``context_window`` marker replay shows for it. Cancel dispatch and status
reads live in services/cancel_dispatch.py and services/thread_status.py.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial

from fastapi import HTTPException

from src.server.handlers.cancellation import cancellation_as_http
from src.server.services.history.projector import context_window_record
from src.server.utils.checkpoint_helpers import (
    build_checkpoint_config,
    get_checkpointer,
    turn_base_checkpoint_id,
    update_at_commit,
)

# Import setup module to access initialized globals
from src.server.app import setup

logger = logging.getLogger(__name__)


async def _resolve_graph_and_state(
    thread_id: str,
    verb: str,
    config=None,
    checkpointer=None,
    user_id=None,
    *,
    held: AsyncExitStack,
) -> tuple:
    """Validate thread, build graph, get state, build backend.

    ``config`` is the resolved AgentConfig; defaults to ``setup.agent_config``.
    ``checkpointer`` overrides the global pooled saver — a mutation passes its
    fence-bound saver so checkpoint writes die with the lock session (I2).
    ``user_id`` identifies the caller to the session acquire, whose MCP resolve
    is owner-scoped. ``held`` is the caller's stack, which keeps the backend's
    folder in place until the caller's block ends.

    Returns:
        (graph, state, messages, workspace_id, backend)
    """
    from src.server.database import conversation as qr_db
    from src.server.database.workspace import get_workspace
    from src.server.database.workspace_folders import (
        WorkspaceFolderMoving,
        is_top_level,
        workspace_folder_in_use,
    )
    from src.server.utils.error_sanitization import sandbox_unreachable_detail
    from src.server.services.workspace_manager import WorkspaceManager
    from ptc_agent.agent.graph import build_ptc_graph_with_session
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.core.paths import SandboxLayout

    # Validate thread + workspace
    thread_info = await qr_db.get_thread_with_summary(thread_id)
    if not thread_info:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")
    workspace_id = thread_info.get("workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail=f"Thread {thread_id} has no associated workspace",
        )
    # The row holds a uuid.UUID; the session and its asset sync key on the str
    # the request paths pass.
    workspace_id = str(workspace_id)

    # Session
    workspace_manager = WorkspaceManager.get_instance()
    try:
        session = await workspace_manager.get_session_for_workspace(
            workspace_id, user_id=user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Graph
    checkpointer = checkpointer if checkpointer is not None else get_checkpointer()
    effective_config = config if config is not None else setup.agent_config
    if not effective_config:
        raise HTTPException(
            status_code=500, detail="Agent configuration not initialized"
        )
    from src.server.services.preview_links import cache_tool_preview

    graph = await build_ptc_graph_with_session(
        session=session,
        tool_view=workspace_manager.tool_view(session, workspace_id),
        config=effective_config,
        checkpointer=checkpointer,
        on_signed_url=partial(cache_tool_preview, workspace_id=workspace_id),
    )

    # State with timeout, read where the next turn would start, so the write
    # lands on the thread's pointer rather than on a branch a fork abandoned.
    base_checkpoint_id = await turn_base_checkpoint_id(checkpointer, thread_id)
    try:
        state = await asyncio.wait_for(
            graph.aget_state(build_checkpoint_config(thread_id, base_checkpoint_id)),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.error(f"aget_state timed out for thread {thread_id} during {verb}")
        raise HTTPException(
            status_code=504,
            detail=f"Timed out retrieving state for thread: {thread_id}",
        )
    if not state or not state.values:
        raise HTTPException(
            status_code=404, detail=f"No state found for thread: {thread_id}"
        )
    messages = state.values.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail=f"No messages to {verb}")

    # Backend. Pinned to the thread's workspace folder because these routes
    # run outside a turn, where nothing has bound a project: an unpinned
    # backend would file this thread's saved attachments on the machine root,
    # which a later delete of the workspace would leave behind. No run keeps a
    # settle off this folder: ``held`` keeps the folder in place, and the row
    # is read under it.
    backend = None
    if hasattr(session, "sandbox") and session.sandbox is not None:
        try:
            await held.enter_async_context(workspace_folder_in_use(workspace_id))
        except WorkspaceFolderMoving as e:
            raise HTTPException(status_code=503, detail=sandbox_unreachable_detail(e)) from None
        row = await get_workspace(workspace_id) or {}
        dir_name = row.get("dir_name")
        if dir_name and not is_top_level(dir_name):
            raise HTTPException(
                status_code=503,
                detail=sandbox_unreachable_detail(WorkspaceFolderMoving(workspace_id)),
            )
        layout = SandboxLayout(session.sandbox.working_dir).for_workspace(dir_name)
        backend = SandboxBackend(session.sandbox, layout.workspace)

    return graph, state, messages, workspace_id, backend


async def _update_graph_state(
    graph, state, values: dict, thread_id: str, verb: str
) -> None:
    """Write ``values`` onto the checkpoint ``state`` was read at, and move
    the thread's commit pointer onto the write when it still points there
    (``update_at_commit``).

    Left beside the pointer, the write is seen by the next turn, which runs
    from the latest checkpoint, but not by anything reading the committed
    branch until that turn ends: replay, and the window's slice coverage.
    """
    built_on = ((state.config or {}).get("configurable") or {}).get("checkpoint_id")
    try:
        advanced = await update_at_commit(
            graph, thread_id, built_on, values, timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.error(f"aupdate_state timed out for thread {thread_id} during {verb}")
        raise HTTPException(
            status_code=504,
            detail=f"Timed out updating state for thread: {thread_id}",
        )
    if not advanced:
        logger.info(
            f"[{verb}] thread {thread_id}: commit pointer is not at {built_on}, "
            "left in place"
        )


async def _summary_since_the_turn(thread_id: str, state):
    """The summary written since the thread's latest turn started, which a
    compact now is about to replace, or None.

    Replay reads a turn's summaries from the state its span ends on, and the
    compact's write becomes that end, so without a copy on the compact's
    marker the turn would lose the one the agent wrote during it, or the one
    an earlier compact after it installed. A summary from before the turn
    shows where it was written. A failed read keeps no copy.
    """
    from langchain_core.messages import HumanMessage

    from src.server.services.history.reader import CheckpointHistoryReader

    values = state.values or {}
    event = values.get("_summarization_event")
    message = event.get("summary_message") if isinstance(event, dict) else None
    if not isinstance(message, HumanMessage) or not message.id:
        return None
    built_on = ((state.config or {}).get("configurable") or {}).get("checkpoint_id")
    try:
        reader = CheckpointHistoryReader.get_instance()
        anchors, _ = await asyncio.wait_for(
            reader.aget_turn_anchors(thread_id, built_on), timeout=10.0
        )
        if not anchors:
            return None
        before = await asyncio.wait_for(
            reader.aget_state(thread_id, anchors[-1].input_checkpoint_id),
            timeout=10.0,
        )
    except Exception:
        logger.warning(
            f"[compact] thread {thread_id}: could not read the latest turn's "
            f"start; a summary it wrote is not kept on the marker",
            exc_info=True,
        )
        return None
    prior = ((before.values or {}) if before else {}).get("_summarization_event")
    prior_message = prior.get("summary_message") if isinstance(prior, dict) else None
    if getattr(prior_message, "id", None) == message.id:
        return None
    return message


@asynccontextmanager
async def _hold_thread_mutation(thread_id: str, verb: str):
    """Hold the exclusive-T mutation fence for a manual /compact|/offload|
    /delete, mapping the runner's refusals onto the HTTP contract (409 with a
    stable code the frontend branches on; 503 on budget exhaustion)."""
    from src.server.services.thread_mutation import (
        MutationConflict,
        MutationUnavailable,
        ThreadMutationRunner,
    )

    runner = ThreadMutationRunner.get_instance()
    try:
        async with runner.exclusive(thread_id, verb) as session:
            yield session
    except MutationConflict as e:
        raise HTTPException(status_code=409, detail=e.detail)
    except MutationUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@cancellation_as_http("compact")
async def trigger_compaction(
    thread_id: str,
    keep_messages: int = 5,
    *,
    user_id: str | None = None,
) -> dict:
    """Manually trigger context compaction for a thread.

    When ``user_id`` is set, applies that user's compaction_model + profile,
    resolved against the thread's own model as its turns are, so manual
    /compact matches the auto path.
    """
    try:
        from ptc_agent.agent.middleware.compaction import compact_messages
        from ptc_agent.agent.middleware.compaction.notes import ThreadScratchpad
        from src.server.app import setup

        # The mutation fence FIRST — before any graph state reads or writes:
        # exclusive T(thread) refuses while a fenced run or tail writer is
        # live (any worker), the ledger gate refuses on an in_progress row,
        # and the op key holds concurrent message POSTs at admission. The
        # runner also owns the user-Stop path (local cancel / cross-worker
        # stop flag).
        async with (
            _hold_thread_mutation(thread_id, "compact") as mutation,
            AsyncExitStack() as held,
        ):
            agent_cfg = setup.agent_config
            if user_id and agent_cfg is not None:
                try:
                    from src.server.database.api_keys import is_byok_active
                    from src.server.database.conversation import get_thread_auth_meta
                    from src.server.services.llm import thread_model
                    from src.server.services.llm.config import resolve_llm_config

                    is_byok = await is_byok_active(user_id)
                    meta = await get_thread_auth_meta(thread_id)

                    async def resolve(model: str | None):
                        return await resolve_llm_config(
                            setup.agent_config,
                            user_id,
                            request_model=model,
                            is_byok=is_byok,
                            thread_id=thread_id,
                        )

                    agent_cfg = await thread_model.resolve_turn_config(
                        user_id,
                        thread_id,
                        named=None,
                        held=meta.get("llm_model") if meta else None,
                        # A summary changes no answer, so it runs on the
                        # default rather than waiting on a reconnect.
                        fall_back=True,
                        resolve=resolve,
                    )
                except HTTPException:
                    # 402 insufficient credits, 403 revoked key, etc. are intentional
                    # user-facing signals — don't silently downgrade to platform config.
                    raise
                except Exception as e:
                    logger.warning(
                        f"[compact] resolve_llm_config failed for user {user_id}: {e}; "
                        "falling back to base agent_config"
                    )
                    agent_cfg = setup.agent_config

            (
                graph, state, messages, workspace_id, backend
            ) = await _resolve_graph_and_state(
                thread_id, "compact", config=agent_cfg,
                checkpointer=mutation.saver, user_id=user_id, held=held,
            )

            # The same pipeline as automatic compaction, on the user's
            # resolved config, so a manual /compact runs the same model and
            # names the notes when the user's flag is on, as the agent build does.
            scratchpad = (
                ThreadScratchpad.resolve(agent_cfg, backend.workspace_dir, thread_id)
                if backend is not None and agent_cfg is not None
                else None
            )
            try:
                compaction = await compact_messages(
                    messages,
                    state.values,
                    agent_cfg,
                    thread_id=thread_id,
                    keep_messages=keep_messages,
                    backend=backend,
                    workspace_id=workspace_id,
                    notes_dir=scratchpad.notes_dir if scratchpad is not None else None,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # The view that was compacted, as automatic compaction counts it,
            # not every message the checkpoint has kept since the thread began.
            original_count = compaction.original_count
            new_message_count = len(compaction.messages)
            summary_text = compaction.summary.text
            summary_length = len(summary_text)
            summary_source = compaction.summary.source

            # The marker rides the write that installs the summary. Action
            # "summarize" is the wire protocol's. Replay reads the text for
            # the "View summary" panel from the summary the marker names.
            marker = context_window_record(
                {
                    "action": "summarize",
                    "signal": "complete",
                    "original_message_count": original_count,
                    "new_message_count": new_message_count,
                    "summary_length": summary_length,
                    "source": summary_source,
                },
                summary_message_id=compaction.event["summary_message"].id,
                replaced=await _summary_since_the_turn(thread_id, state),
            )
            await _update_graph_state(
                graph,
                state,
                {**compaction.update(state.values), "ui": [marker]},
                thread_id,
                "compact",
            )

            logger.info(
                f"Manual compaction completed for thread {thread_id}: "
                f"{original_count} -> {new_message_count} messages"
            )

            return {
                "success": True,
                "thread_id": thread_id,
                "original_message_count": original_count,
                "new_message_count": new_message_count,
                "summary_length": summary_length,
                "summary_text": summary_text,
                "source": summary_source,
            }

    except HTTPException:
        raise
    except Exception as e:
        # CancelledError (user Stop / client disconnect) is handled by the
        # @cancellation_as_http wrapper, which sees it after the mutation
        # fence releases.
        logger.exception(f"Error triggering compaction for thread {thread_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to trigger compaction: {str(e)}"
        )


@cancellation_as_http("offload")
async def trigger_offload(thread_id: str, *, user_id: str | None = None) -> dict:
    """
    Manually trigger tool-arg offloading for a thread (Tier 1 only).

    Records large tool arguments and stale Read results in older messages as
    offloaded, an argument only once the transcript that keeps it is saved.
    No LLM summarization is performed. ``user_id`` identifies the caller to the
    session acquire, same as
    :func:`trigger_compaction`.

    Args:
        thread_id: The thread/conversation ID to offload

    Returns:
        Dict with success, thread_id, message_count, offloaded_args, offloaded_reads
    """
    try:
        from ptc_agent.agent.middleware.compaction import (
            OffloadSettings,
            offload_tool_args,
            record_offloads,
        )

        # Same fence as /compact — /offload also writes checkpoint state and
        # could race a running workflow's _offloaded_tool_call_ids updates.
        # The exclusive-T lock + ledger gate are deterministic, so the old
        # fail-open/fail-closed tracker asymmetry is gone.
        async with (
            _hold_thread_mutation(thread_id, "offload") as mutation,
            AsyncExitStack() as held,
        ):
            (
                graph, state, messages, workspace_id, backend
            ) = await _resolve_graph_and_state(
                thread_id, "offload", checkpointer=mutation.saver, user_id=user_id, held=held
            )

            # Set: resolving the graph above refuses to run without it.
            settings = OffloadSettings.from_config(setup.agent_config.compaction)
            try:
                offloaded_arg_ids, offloaded_read_ids = await offload_tool_args(
                    messages,
                    state.values,
                    settings,
                    thread_id=thread_id,
                    backend=backend,
                    workspace_id=workspace_id,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            offloaded_args = len(offloaded_arg_ids)
            offloaded_reads = len(offloaded_read_ids)

            # Ids only, never messages: the checkpoint keeps every message and
            # the middleware applies the recorded ids on every call. Unlike
            # the automatic pass, this one does not wait for an idle turn: the
            # user asked for it now. The marker rides the same write.
            marker = context_window_record(
                {
                    "action": "offload",
                    "signal": "complete",
                    "offloaded_args": offloaded_args,
                    "offloaded_reads": offloaded_reads,
                }
            )
            await _update_graph_state(
                graph,
                state,
                {
                    **record_offloads(
                        state.values, offloaded_arg_ids, offloaded_read_ids
                    ),
                    "ui": [marker],
                },
                thread_id,
                "offload",
            )

            logger.info(
                f"Manual offload completed for thread {thread_id}: "
                f"{offloaded_args} tool args, {offloaded_reads} read results"
            )

            return {
                "success": True,
                "thread_id": thread_id,
                "message_count": len(messages),
                "offloaded_args": offloaded_args,
                "offloaded_reads": offloaded_reads,
            }

    except HTTPException:
        raise
    except Exception as e:
        # CancelledError (user Stop / client disconnect) is handled by the
        # @cancellation_as_http wrapper, which sees it after the mutation
        # fence releases.
        logger.exception(f"Error triggering offload for thread {thread_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to trigger offload: {str(e)}"
        )

