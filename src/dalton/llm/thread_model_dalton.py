"""Dalton helpers for thread model switching, atomic Antigravity effort clear, and SSE broadcasts.

Keeps upstream ``src/server/services/llm/thread_model.py`` and
``src/server/app/threads/crud.py`` limited to thin ``DALTON-HOOK`` calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.dalton.llm.antigravity_series import is_builtin_antigravity_variant


async def resolve_antigravity_effort_clear(
    user_id: str,
    model: str | None,
    *,
    model_pref: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when writing ``model`` must atomically clear ``reasoning_effort``.

    Built-in Antigravity variants encode reasoning effort in their model key
    (e.g. ``gemini-4-neon-low-oauth``). Leaving a stale standard
    ``reasoning_effort`` on the thread row causes subsequent turns that omit
    ``reasoning_effort`` to remap the variant (e.g. Low -> High).
    """
    if not model:
        return False
    pref: Mapping[str, Any] | None = model_pref
    if pref is None:
        from src.server.services.llm import user_models

        try:
            pref = await user_models.get_model_preference(user_id)
        except RuntimeError:
            pref = None
    return is_builtin_antigravity_variant(model, model_pref=pref)


async def apply_antigravity_effort_clear(
    user_id: str,
    model: str | None,
    updates: dict[str, Any],
    *,
    model_pref: Mapping[str, Any] | None = None,
) -> bool:
    """Atomically add ``reasoning_effort = None`` to ``updates`` when switching
    to a built-in Antigravity variant and ``reasoning_effort`` was not already
    supplied by the caller.

    Returns True when ``reasoning_effort`` was set to ``None`` for an
    Antigravity variant.
    """
    if "reasoning_effort" in updates:
        return False
    should_clear = await resolve_antigravity_effort_clear(
        user_id, model, model_pref=model_pref
    )
    if should_clear:
        updates["reasoning_effort"] = None
    return should_clear


def enforce_conditional_effort_clear_guard(
    updates: Mapping[str, Any],
    cas_guards: dict[str, Any],
) -> None:
    """Ensure any conditional PATCH that clears ``reasoning_effort`` guards it.

    Unconditional manual clicks have empty ``cas_guards`` and are untouched.
    When a background conditional PATCH (e.g. carrying ``expected_llm_model``)
    also writes ``reasoning_effort`` (including server-side Antigravity clear)
    without an explicit ``expected_reasoning_effort``, default the guard to
    ``None`` so a concurrent writer's non-null effort is never wiped silently.
    """
    if (
        cas_guards
        and "reasoning_effort" in updates
        and "expected_reasoning_effort" not in cas_guards
    ):
        cas_guards["expected_reasoning_effort"] = None


async def resolve_thread_row_effort_for_model(
    user_id: str,
    *,
    model: str | None,
    row_model: str | None,
    row_effort: str | None,
) -> str | None:
    """Decide whether a thread row's stored ``reasoning_effort`` applies to ``model``.

    When a request omits ``reasoning_effort``, never inherit the thread row's
    stored effort onto a different explicitly requested model (e.g. a queued
    message naming ``Neon Low`` or another standard model after another tab
    updated the thread row to ``gpt-6.2-sol`` + ``high``) or onto any built-in
    Antigravity variant (whose effort is encoded in the model key).
    """
    if row_effort is None:
        return None
    if model is not None and model != row_model:
        return None
    if model is not None and await resolve_antigravity_effort_clear(user_id, model):
        return None
    return row_effort


async def publish_thread_model_switch_events(
    *,
    user_id: str,
    thread_id: str,
    updated_thread: Mapping[str, Any],
    cleared_effort: bool,
) -> None:
    """Publish ``thread_model`` (and ``thread_tuning`` when effort was cleared)
    on the user's SSE lifecycle feed after a thread model switch commits."""
    from src.server.services.thread_lifecycle_feed import (
        publish_thread_model,
        publish_thread_tuning,
    )

    workspace_id = (
        str(updated_thread["workspace_id"])
        if updated_thread.get("workspace_id")
        else None
    )
    await publish_thread_model(
        user_id=user_id,
        thread_id=thread_id,
        workspace_id=workspace_id,
        llm_model=updated_thread.get("llm_model"),
    )
    if cleared_effort:
        await publish_thread_tuning(
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=workspace_id,
            reasoning_effort=updated_thread.get("reasoning_effort"),
            fast_mode=updated_thread.get("fast_mode"),
        )
