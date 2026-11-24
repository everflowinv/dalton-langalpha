"""Handler-tier orchestration for immediate and deferred model-switch handoffs.

Lives in ``handlers/`` because it drives ``handoff_brief`` and
``thread_maintenance`` (which hold agent-build / checkpoint-mutation logic),
keeping ``services/llm/thread_model.py`` free of ``services -> handlers``
imports. Registered with ``thread_model`` at server startup.
"""

from __future__ import annotations

import logging
from typing import Any

from src.server.services.llm import thread_model as thread_model_svc
from src.server.services.llm.thread_model import (
    HandoffOutcome,
    ThreadModelSwitchPlan,
    clear_pending_model_handoff,
    read_pending_model_handoff_context,
    register_handoff_runners,
)

logger = logging.getLogger(__name__)


async def _has_pending_interrupt(thread_id: str) -> bool:
    """Whether the thread's base checkpoint has an unresolved HITL interrupt."""
    from fastapi import HTTPException
    from src.server.utils.checkpoint_helpers import (
        build_checkpoint_config,
        get_checkpointer,
        interrupt_records,
        turn_base_checkpoint_id,
    )

    try:
        checkpointer = get_checkpointer()
    except HTTPException as exc:
        if exc.status_code == 500 and exc.detail == "Checkpointer not initialized":
            return False
        raise
    base = await turn_base_checkpoint_id(checkpointer, thread_id)
    saved = await checkpointer.aget_tuple(build_checkpoint_config(thread_id, base))
    return bool(saved and interrupt_records(saved.pending_writes))


async def inspect_hand_off(thread_id: str, user_id: str, model: str | None):
    """Classify whether ``model`` needs to and can hand off an opaque summary:
    returns ``("ready", (config, route))``, ``("not_needed", None)``, or
    ``("retryable_failure", None)`` when reading or resolving ``model`` fails
    or when the checkpoint is paused on an unresolved user interrupt."""
    from src.llms.provider_compaction import replays
    from src.server.handlers.thread_maintenance import (
        _resolve_accepts_ignore_custom_override,  # DALTON-HOOK(cos-settings-tools)
        _resolver,
        _summary_to_hand_off,
        thread_route,
    )

    try:
        summary = await _summary_to_hand_off(thread_id)
        if summary is None and await _has_pending_interrupt(thread_id):
            return "retryable_failure", None
    except Exception:
        logger.warning(f"[handoff] thread {thread_id}: switch not read", exc_info=True)
        return "retryable_failure", None
    if summary is None:
        return "not_needed", None
    try:
        meta, route = await thread_route(thread_id, user_id)
        resolve = await _resolver(user_id, thread_id, meta, route)
    except Exception:
        logger.warning(f"[handoff] thread {thread_id}: switch not read", exc_info=True)
        return "retryable_failure", None
    try:
        # DALTON-HOOK(cos-settings-tools): resolve the newly switched-to model under the owner's own selection
        if _resolve_accepts_ignore_custom_override(resolve):
            switched_to = (
                await resolve(meta.get("llm_model"), ignore_custom_models_override=False)
            ).get_llm_client()
        else:
            switched_to = (await resolve(meta.get("llm_model"))).get_llm_client()
    except Exception:
        # A model that cannot run now reads the text when it can.
        switched_to = None
    if switched_to is not None and replays(summary, switched_to):
        return "not_needed", None
    try:
        config = await resolve(model)
        readable = replays(summary, config.get_llm_client())
    except Exception as e:
        logger.info(f"[handoff] thread {thread_id}: {model!r} cannot run ({e})")
        return "retryable_failure", None
    if not readable:
        logger.info(f"[handoff] thread {thread_id}: {model!r} cannot read the summary")
        return "not_needed", None
    return "ready", (config, route)


async def _brief_handoff_still_owed(thread_id: str) -> bool:
    from ptc_agent.agent.middleware.compaction.brief import brief_owed
    from src.server.services.history.reader import CheckpointHistoryReader
    from src.server.utils.checkpoint_helpers import (
        get_checkpointer,
        turn_base_checkpoint_id,
    )

    base = await turn_base_checkpoint_id(get_checkpointer(), thread_id)
    state = await CheckpointHistoryReader.get_instance().aget_state(thread_id, base)
    return brief_owed((state.values or {}) if state else {})


async def run_model_switch_handoff(
    user_id: str,
    thread_id: str,
    workspace_id: str | None,
    previous_model: str | None,
    model: str | None,
) -> tuple[bool | None, bool | None]:
    """Trigger immediate brief or summary hand-off for a committed model
    switch, returning ``(brief_handoff, summary_handoff)``.
    """
    from src.server.handlers.handoff_brief import brief_on_switch
    from src.server.handlers.thread_maintenance import hand_off_on_switch

    try:
        to_claude_code = await thread_model_svc.next_turn_runs_claude_code(
            user_id, str(workspace_id or ""), model
        )
    except Exception:
        logger.warning(
            f"[handoff] thread {thread_id}: switch routing not read",
            exc_info=True,
        )
        return None, None
    if to_claude_code:
        brief_handoff = await brief_on_switch(thread_id, user_id, model)
        return brief_handoff, None
    summary_handoff = await hand_off_on_switch(thread_id, user_id, previous_model)
    return None, summary_handoff


async def run_immediate_model_handoff(
    user_id: str,
    thread_id: str,
    workspace_id: str | None,
    plan: ThreadModelSwitchPlan,
) -> tuple[bool | None, bool | None]:
    """Registered runner for immediate ``switch_thread_model`` calls."""
    return await run_model_switch_handoff(
        user_id,
        thread_id,
        workspace_id,
        plan.pending_previous_model,
        plan.model,
    )


async def clear_pending_handoff_after_brief(
    thread_id: str, pending_marker: dict[str, Any]
) -> None:
    """Best-effort versioned clear after an in-turn Claude Code brief succeeds."""
    try:
        await clear_pending_model_handoff(
            thread_id, version=pending_marker.get("version")
        )
    except Exception:
        logger.warning(
            f"[handoff] thread {thread_id}: failed clearing pending handoff after in-turn brief",
            exc_info=True,
        )


# DALTON-HOOK(cos-settings-tools): capture latest system-only run id at handoff start and atomically clear pending marker + stamp owner_model_switch_run_id only if that run id is still latest
async def _read_initial_system_only_run_id(thread_id: str) -> str | None:
    try:
        from collections.abc import Mapping

        from src.server.database.runs import lifecycle as _runs_lifecycle

        latest = await _runs_lifecycle.get_latest_attempt(thread_id)
        if (
            isinstance(latest, Mapping)
            and (latest.get("metadata") or {}).get("llm_model_system_only")
            and latest.get("conversation_response_id")
        ):
            raw_id = str(latest["conversation_response_id"]).strip()
            return raw_id or None
    except Exception:
        logger.debug(
            "[handoff] thread %s: failed reading initial system-only run id",
            thread_id,
            exc_info=True,
        )
    return None


async def _settle_pending_model_handoff(
    thread_id: str,
    *,
    version: str | None,
    expected_system_only_run_id: str | None = None,
) -> None:
    if not expected_system_only_run_id:
        await clear_pending_model_handoff(thread_id, version=version)
        return
    from src.dalton.database import model_switch as dalton_model_switch_db

    await dalton_model_switch_db.settle_pending_model_handoff_metadata(
        thread_id,
        thread_model_svc.PENDING_MODEL_HANDOFF_KEY,
        expected_version=version,
        switch_run_id_key="owner_model_switch_run_id",
        expected_run_id=expected_system_only_run_id,
    )


async def run_pending_model_handoff(
    thread_id: str,
    *,
    user_id: str | None = None,
) -> HandoffOutcome:
    """Execute a deferred model-switch handoff once no turn holds the thread.

    Returns one of ``"completed"``, ``"not_needed"``, or ``"retryable_failure"``.
    Clears ``PENDING_MODEL_HANDOFF_KEY`` (conditional on the read ``version``)
    only on ``"completed"`` or ``"not_needed"``. Retryable failures (including
    transient errors resolving ``previous_model`` or connection failures) log a
    warning and leave the marker intact.
    """
    from src.server.handlers.handoff_brief import _budget as brief_handoff_budget, brief_on_switch
    from src.server.handlers.thread_maintenance import hand_off_on_switch
    from src.server.services.thread_mutation import ThreadMutationRunner

    ctx, early_outcome = await read_pending_model_handoff_context(
        thread_id, user_id=user_id
    )
    if ctx is None:
        return early_outcome

    # DALTON-HOOK(cos-settings-tools): snapshot latest system-only run id before handoff work so settle never stamps a run that started mid-handoff
    initial_system_only_run_id = await _read_initial_system_only_run_id(thread_id)

    try:
        to_claude_code = await thread_model_svc.next_turn_runs_claude_code(
            ctx.user_id, str(ctx.workspace_id or ""), ctx.current_model
        )
        if to_claude_code:
            if await _has_pending_interrupt(thread_id):
                logger.warning(
                    f"[handoff] thread {thread_id}: checkpoint has pending interrupt; keeping pending marker"
                )
                return "retryable_failure"
            owed = await _brief_handoff_still_owed(thread_id)
            if not owed:
                await _settle_pending_model_handoff(
                    thread_id,
                    version=ctx.version,
                    expected_system_only_run_id=initial_system_only_run_id,
                )
                return "not_needed"
            started = await brief_on_switch(
                thread_id, ctx.user_id, ctx.current_model
            )
            if not started:
                try:
                    still_owed = await _brief_handoff_still_owed(thread_id)
                except Exception:
                    still_owed = True
                if not still_owed:
                    await _settle_pending_model_handoff(
                        thread_id,
                        version=ctx.version,
                        expected_system_only_run_id=initial_system_only_run_id,
                    )
                    return "not_needed"
                logger.warning(
                    f"[handoff] thread {thread_id}: deferred brief could not start; keeping pending marker"
                )
                return "retryable_failure"
            idle = await ThreadMutationRunner.get_instance().wait_until_idle(
                thread_id, timeout=brief_handoff_budget()
            )
            if not idle:
                logger.warning(
                    f"[handoff] thread {thread_id}: timed out waiting for deferred brief"
                )
                return "retryable_failure"
            if await _brief_handoff_still_owed(thread_id):
                logger.warning(
                    f"[handoff] thread {thread_id}: deferred brief still owed; keeping pending marker"
                )
                return "retryable_failure"
        else:
            # Try every model that may have written the owed summary; the first
            # that finds something to hand off is the source.
            sources = list(ctx.candidate_models) or [ctx.previous_model]
            source = sources[0]
            pre_status = "not_needed"
            saw_retryable = False
            for candidate in sources:
                status, _ = await inspect_hand_off(thread_id, ctx.user_id, candidate)
                if status == "ready":
                    source, pre_status = candidate, status
                    break
                if status != "not_needed":
                    saw_retryable = True
            else:
                if saw_retryable:
                    pre_status = "retryable_failure"
            if pre_status == "not_needed":
                # DALTON-HOOK(cos-settings-tools): settle pending marker atomically with guarded switch run id
                await _settle_pending_model_handoff(
                    thread_id,
                    version=ctx.version,
                    expected_system_only_run_id=initial_system_only_run_id,
                )
                return "not_needed"
            if pre_status != "ready":
                logger.warning(
                    f"[handoff] thread {thread_id}: deferred summary handoff not ready ({pre_status}); keeping pending marker"
                )
                return "retryable_failure"
            started = await hand_off_on_switch(
                thread_id, ctx.user_id, source
            )
            if not started:
                recheck_status, _ = await inspect_hand_off(
                    thread_id, ctx.user_id, source
                )
                if recheck_status == "not_needed":
                    await _settle_pending_model_handoff(
                        thread_id,
                        version=ctx.version,
                        expected_system_only_run_id=initial_system_only_run_id,
                    )
                    return "not_needed"
                logger.warning(
                    f"[handoff] thread {thread_id}: deferred summary handoff could not start ({recheck_status}); keeping pending marker"
                )
                return "retryable_failure"
            idle = await ThreadMutationRunner.get_instance().wait_until_idle(
                thread_id, timeout=brief_handoff_budget()
            )
            if not idle:
                logger.warning(
                    f"[handoff] thread {thread_id}: timed out waiting for deferred summary handoff"
                )
                return "retryable_failure"
            post_status, _ = await inspect_hand_off(
                thread_id, ctx.user_id, source
            )
            if post_status != "not_needed":
                logger.warning(
                    f"[handoff] thread {thread_id}: deferred summary handoff not settled ({post_status}); keeping pending marker"
                )
                return "retryable_failure"

        # DALTON-HOOK(cos-settings-tools): settle pending marker atomically with guarded switch run id
        await _settle_pending_model_handoff(
            thread_id,
            version=ctx.version,
            expected_system_only_run_id=initial_system_only_run_id,
        )
        return "completed"
    except Exception:
        logger.warning(
            f"[handoff] thread {thread_id}: deferred model handoff failed; keeping pending marker",
            exc_info=True,
        )
        return "retryable_failure"


def register_thread_model_handoff_runners() -> None:
    """Wire handler-tier handoff runners into ``services.llm.thread_model``."""
    register_handoff_runners(
        immediate=run_immediate_model_handoff,
        pending=run_pending_model_handoff,
    )
