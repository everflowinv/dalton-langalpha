"""Privacy boundary helpers for personal watchlist context and tools (Item 15)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_PERSONAL_WATCHLIST_TEXT_MARKERS: tuple[str, ...] = (
    "<watchlist_context>",
    "personal watchlist snapshot",
    "[personal watchlist",
    "[个人关注列表",
    "personal_watchlist",
    "当前个人关注列表",
    "个人关注列表上下文",
    "personal watchlist context",
)

_PERSONAL_WATCHLIST_WIDGET_TYPES: frozenset[str] = frozenset(
    {
        "personal_watchlist",
        "dalton_watchlist",
        "coverage_watchlist",
    }
)


def _attr_or_key(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def is_personal_watchlist_turn_allowed(
    *,
    own_data: bool = True,
    is_shared: bool = False,
    user_id: str | None = None,
    owner_id: str | None = None,
    workspace_owner_id: str | None = None,
) -> bool:
    """Return True iff the current turn is in the caller's own private workspace/Home.

    Personal watchlist tools (`dalton_coverage`) and personal watchlist chat context
    are strictly forbidden in shared workspaces/threads and colleague workspaces.
    """
    if is_shared or not own_data:
        return False
    caller = str(user_id or "").strip()
    ws_owner = str(workspace_owner_id or owner_id or "").strip()
    if caller and ws_owner and caller != ws_owner:
        return False
    return True


def is_personal_watchlist_context_item(ctx: Any) -> bool:
    """Return True if an ``additional_context`` item carries personal watchlist data."""
    if ctx is None:
        return False
    ctx_type = str(_attr_or_key(ctx, "type") or "").strip().lower()
    if ctx_type in ("watchlist", "personal_watchlist", "coverage"):
        return True
    if ctx_type == "widget":
        widget_type = str(_attr_or_key(ctx, "widget_type") or "").strip().lower()
        if widget_type in _PERSONAL_WATCHLIST_WIDGET_TYPES:
            return True
        data = _attr_or_key(ctx, "data")
        if isinstance(data, Mapping):
            tab = str(data.get("tab") or "").strip().lower()
            if tab == "coverage":
                return True
            if str(data.get("source") or "").strip().lower() in _PERSONAL_WATCHLIST_WIDGET_TYPES:
                return True
        return False
    if ctx_type == "text":
        content = str(_attr_or_key(ctx, "content") or "").strip().lower()
        if any(marker in content for marker in _PERSONAL_WATCHLIST_TEXT_MARKERS):
            return True
    return False


def strip_watchlist_context_if_shared(
    additional_context: Sequence[Any] | None,
    *,
    own_data: bool = True,
    is_shared: bool = False,
    user_id: str | None = None,
    owner_id: str | None = None,
    workspace_owner_id: str | None = None,
) -> list[Any] | None:
    """Strip client-submitted personal watchlist context on shared or foreign turns."""
    if additional_context is None:
        return None
    if is_personal_watchlist_turn_allowed(
        own_data=own_data,
        is_shared=is_shared,
        user_id=user_id,
        owner_id=owner_id,
        workspace_owner_id=workspace_owner_id,
    ):
        return list(additional_context)
    return [item for item in additional_context if not is_personal_watchlist_context_item(item)]


__all__ = [
    "is_personal_watchlist_context_item",
    "is_personal_watchlist_turn_allowed",
    "strip_watchlist_context_if_shared",
]
