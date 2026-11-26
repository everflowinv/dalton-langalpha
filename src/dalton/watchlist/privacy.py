"""Privacy boundary helpers, inline context injection, and signal routing for Personal Watchlist."""

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
    thread_is_shared: bool = False,
    user_id: str | None = None,
    owner_id: str | None = None,
    workspace_owner_id: str | None = None,
) -> bool:
    """Return True iff the current turn is in the caller's own private workspace/Home and unshared thread."""
    if is_shared or thread_is_shared or not own_data:
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
    if ctx_type in ("watchlist_context", "watchlist", "personal_watchlist", "coverage"):
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
    thread_is_shared: bool = False,
    user_id: str | None = None,
    owner_id: str | None = None,
    workspace_owner_id: str | None = None,
) -> list[Any] | None:
    """Strip client-submitted personal watchlist context on shared workspaces/threads or foreign turns."""
    if additional_context is None:
        return None
    if is_personal_watchlist_turn_allowed(
        own_data=own_data,
        is_shared=is_shared,
        thread_is_shared=thread_is_shared,
        user_id=user_id,
        owner_id=owner_id,
        workspace_owner_id=workspace_owner_id,
    ):
        return list(additional_context)
    return [item for item in additional_context if not is_personal_watchlist_context_item(item)]


async def _is_thread_publicly_shared(thread_id: str | None) -> bool:
    tid = str(thread_id or "").strip()
    if not tid or tid == "__default__":
        return False
    try:
        from src.server.database.conversation.threads_read import get_thread_share_status

        info = await get_thread_share_status(tid)
        if isinstance(info, Mapping):
            return bool(info.get("is_shared"))
    except Exception:
        pass
    return False


async def sanitize_turn_watchlist_context(
    request: Any,
    *,
    thread_id: str | None = None,
    is_shared: bool = False,
    user_id: str | None = None,
    workspace_owner_id: str | None = None,
) -> None:
    """Strip personal watchlist context from ``request.additional_context`` if workspace or thread is shared."""
    raw = getattr(request, "additional_context", None)
    if not raw:
        return
    has_wl = any(is_personal_watchlist_context_item(item) for item in raw)
    if not has_wl:
        return
    thread_shared = await _is_thread_publicly_shared(thread_id) if not is_shared else True
    request.additional_context = strip_watchlist_context_if_shared(
        raw,
        is_shared=is_shared,
        thread_is_shared=thread_shared,
        user_id=user_id,
        workspace_owner_id=workspace_owner_id,
    )


def build_watchlist_context_reminder(
    additional_context: Sequence[Any] | None,
) -> str | None:
    """Build an inline ``<system-reminder>`` block from allowed ``WatchlistContext`` items."""
    if not additional_context:
        return None
    parts: list[str] = []
    for ctx in additional_context:
        ctx_type = str(_attr_or_key(ctx, "type") or "").strip().lower()
        if ctx_type != "watchlist_context":
            continue
        content = str(_attr_or_key(ctx, "content") or "").strip()
        if content:
            parts.append(content)
    if not parts:
        return None
    return "\n\n<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"


def dispatch_price_monitor_signal(service: Any, raw_data: Any) -> None:
    """Route Redis target-change pub/sub messages to automation reload vs target reload."""
    if isinstance(raw_data, bytes):
        raw_data = raw_data.decode("utf-8", errors="ignore")
    if str(raw_data or "").strip() == "__automations__":
        service.request_automations_reload()
    else:
        service.request_targets_reload(raw_data)


__all__ = [
    "build_watchlist_context_reminder",
    "dispatch_price_monitor_signal",
    "is_personal_watchlist_context_item",
    "is_personal_watchlist_turn_allowed",
    "sanitize_turn_watchlist_context",
    "strip_watchlist_context_if_shared",
]
