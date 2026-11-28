"""Mutual exclusion between upstream ``delivery_config.methods`` and Dalton notify routing.

An automation cannot enable both upstream channel delivery (``delivery_config.methods``)
and Dalton push notifications (built-in ``dalton-*`` classification, ``report_profile``,
or a non-empty ``dalton_notify_routes`` entry) at the same time; otherwise a single
completed execution would deliver twice through independent ledgers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import NonCallableMock

from psycopg.rows import dict_row

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.database.automation_reports import (
    automation_has_validated_report_profile,
    held_delivery_configured,
)

BUILTIN_DELIVERY_CONFLICT_MSG = (
    "此任务的推送由 Dalton 通知设置管理，不能同时配置渠道投递（delivery_config.methods）。"
)
REPORT_PROFILE_DELIVERY_CONFLICT_MSG = (
    "此任务已配置报告外发（report_profile），不能同时配置渠道投递（delivery_config.methods）。"
)
NOTIFY_ROUTE_DELIVERY_CONFLICT_MSG = (
    "此任务已配置 Dalton 通知推送路由，不能同时启用渠道投递（delivery_config.methods）；"
    "请先清空其中一侧的推送配置。"
)
ROUTE_HELD_DELIVERY_CONFLICT_MSG = (
    "此任务已配置渠道投递（delivery_config.methods），不能同时启用 Dalton 通知推送路由；"
    "请先在任务编辑页清空推送渠道后再配置 Dalton 推送路由。"
)


def _has_non_empty_targets(targets: Any) -> bool:
    return (
        isinstance(targets, Sequence)
        and not isinstance(targets, (str, bytes))
        and any(isinstance(t, str) and t.strip() for t in targets)
    )


def has_active_dalton_notify_route(
    automation: Mapping[str, Any] | None,
    routes: Mapping[str, Any] | None,
) -> bool:
    """Return True when ``routes`` holds a non-empty target list for ``automation``."""
    if not isinstance(automation, Mapping) or not isinstance(routes, Mapping):
        return False
    auto_id = str(automation.get("automation_id") or "").strip()
    if not auto_id:
        return False
    route_key = f"automation:{auto_id}"
    return _has_non_empty_targets(routes.get(route_key))


def execution_has_upstream_delivery(
    execution_delivery: Any = None,
    execution_delivery_result: Any = None,
) -> bool:
    """Return True when this execution already registered or performed upstream delivery.

    Checks the execution's own stamped ``delivery`` record (held gateway targets)
    and ``delivery_result`` (confirmed or webhook-dispatched entries), rather than
    the automation's mutable current ``delivery_config``.
    """
    if execution_delivery is not None:
        held_attr = getattr(execution_delivery, "held", None)
        if isinstance(held_attr, bool):
            if held_attr:
                return True
        elif isinstance(execution_delivery, Mapping):
            targets = execution_delivery.get("targets")
            if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes)):
                if any(isinstance(t, Mapping) and t.get("ok") is True for t in targets):
                    return True

    if isinstance(execution_delivery_result, Sequence) and not isinstance(
        execution_delivery_result, (str, bytes)
    ):
        for item in execution_delivery_result:
            if not isinstance(item, Mapping):
                continue
            if item.get("success") is True:
                return True
            if item.get("via") in ("agent", "fallback", "notice"):
                return True
            # WebhookClient.fire_event returns {"method": ..., "success": bool} without error/via.
            if (
                "error" not in item
                and "via" not in item
                and isinstance(item.get("method"), str)
                and item["method"].strip()
            ):
                return True
    return False


def check_route_held_delivery_conflict(
    automation: Mapping[str, Any] | None,
    targets: Any,
) -> str | None:
    """Return a human-readable conflict message if setting ``targets`` conflicts with ``delivery_config.methods``."""
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return None
    if not isinstance(automation, Mapping):
        return None
    if not _has_non_empty_targets(targets):
        return None
    if held_delivery_configured(automation.get("delivery_config")):
        return ROUTE_HELD_DELIVERY_CONFLICT_MSG
    return None


async def _load_dalton_notify_routes_in_tx(
    user_id: str,
    *,
    conn: Any,
) -> dict[str, Any] | None:
    """Read ``dalton_notify_routes`` from ``user_preferences`` on ``conn`` inside the caller's transaction.

    Returns ``None`` only when ``conn`` is an in-memory test double whose cursor
    does not implement ``fetchone``.
    """
    from src.dalton.llm.model_prefs import (
        DALTON_NOTIFY_ROUTES_KEY,
        _normalize_stored_value,
    )

    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        return {}
    async with conn.cursor(row_factory=dict_row) as cur:
        if not hasattr(cur, "fetchone"):
            return None
        await cur.execute(
            "SELECT model_preference FROM user_preferences WHERE user_id = %s",
            (cleaned_uid,),
        )
        row = await cur.fetchone()
    if not isinstance(row, Mapping):
        return {}
    mp = row.get("model_preference")
    if not isinstance(mp, Mapping) or DALTON_NOTIFY_ROUTES_KEY not in mp:
        return {}
    normalized = _normalize_stored_value(
        DALTON_NOTIFY_ROUTES_KEY, mp.get(DALTON_NOTIFY_ROUTES_KEY)
    )
    return dict(normalized) if isinstance(normalized, Mapping) else {}


_UNSET_ROUTES = object()


async def assert_no_automation_delivery_notify_conflict(
    *,
    user_id: str,
    automation: Mapping[str, Any],
    delivery_config: Any,
    metadata: Mapping[str, Any] | None = None,
    notify_routes: Any = _UNSET_ROUTES,
    model_pref: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Single mutex check on an automation's post-save final state.

    Validates the state that will exist once the enclosing save transaction commits:
    - ``delivery_config``: post-save channel delivery config;
    - ``metadata``: post-save metadata (defaults to ``automation.get("metadata")``);
    - ``notify_routes``: post-save Dalton notify targets for this automation
      (``Sequence[str]``), a full ``dalton_notify_routes`` mapping, ``None`` when
      cleared in the same transaction, or omitted to consult any
      ``post_save_notify_routes`` bound on ``automation_history_context`` before
      falling back to stored routes via ``conn`` / ``model_pref``.
    """
    if not held_delivery_configured(delivery_config):
        return

    from src.dalton.llm.model_prefs import (
        DALTON_NOTIFY_ROUTES_KEY,
        resolve_dalton_notify_routes,
    )
    from src.dalton.notify.dispatch import (
        classify_automation_for_notify,
        is_excluded_automation_for_notify,
    )
    from src.server.services.automations.lifecycle import AutomationRefusal

    final_automation: Mapping[str, Any] = (
        {**automation, "metadata": metadata}
        if metadata is not None
        else automation
    )

    if automation_has_validated_report_profile(final_automation):
        raise AutomationRefusal(
            REPORT_PROFILE_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )

    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return

    if (
        not is_excluded_automation_for_notify(final_automation)
        and classify_automation_for_notify(final_automation) is not None
    ):
        raise AutomationRefusal(
            BUILTIN_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )

    auto_id = str(final_automation.get("automation_id") or "").strip()
    route_key = f"automation:{auto_id}" if auto_id else ""

    has_route = False
    if notify_routes is not _UNSET_ROUTES:
        if notify_routes is None:
            has_route = False
        elif isinstance(notify_routes, Mapping):
            has_route = has_active_dalton_notify_route(final_automation, notify_routes)
        else:
            has_route = _has_non_empty_targets(notify_routes)
    else:
        from src.dalton.services.company_automations import (
            get_current_history_context,
        )

        hist_ctx = get_current_history_context()
        pending_routes = (
            hist_ctx.post_save_notify_routes
            if hist_ctx is not None
            and isinstance(hist_ctx.post_save_notify_routes, Mapping)
            else None
        )
        matched_pending = False
        if pending_routes is not None and route_key:
            if route_key in pending_routes:
                has_route = _has_non_empty_targets(pending_routes.get(route_key))
                matched_pending = True
            elif route_key.lower() in pending_routes:
                has_route = _has_non_empty_targets(
                    pending_routes.get(route_key.lower())
                )
                matched_pending = True
        if not matched_pending and auto_id:
            routes: Mapping[str, Any] | None = None
            if (
                conn is not None
                and hasattr(conn, "cursor")
                and not isinstance(conn, NonCallableMock)
            ):
                routes = await _load_dalton_notify_routes_in_tx(user_id, conn=conn)
            if routes is None:
                if isinstance(model_pref, Mapping):
                    raw_routes = model_pref.get(DALTON_NOTIFY_ROUTES_KEY)
                    routes = raw_routes if isinstance(raw_routes, Mapping) else {}
                else:
                    routes = await resolve_dalton_notify_routes(user_id)
            has_route = has_active_dalton_notify_route(final_automation, routes)

    if has_route:
        raise AutomationRefusal(
            NOTIFY_ROUTE_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )
