"""Mutual exclusion between upstream ``delivery_config.methods`` and Dalton notify routing.

An automation cannot enable both upstream channel delivery (``delivery_config.methods``)
and Dalton push notifications (built-in ``dalton-*`` classification, ``report_profile``,
or a non-empty ``dalton_notify_routes`` entry) at the same time; otherwise a single
completed execution would deliver twice through independent ledgers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def check_route_held_delivery_conflict(
    automation: Mapping[str, Any] | None,
    targets: Any,
) -> str | None:
    """Return a human-readable conflict message if setting ``targets`` conflicts with ``delivery_config.methods``."""
    if not isinstance(automation, Mapping):
        return None
    if not _has_non_empty_targets(targets):
        return None
    if held_delivery_configured(automation.get("delivery_config")):
        return ROUTE_HELD_DELIVERY_CONFLICT_MSG
    return None


async def assert_no_automation_delivery_notify_conflict(
    *,
    user_id: str,
    automation: Mapping[str, Any],
    delivery_config: Any,
    model_pref: Mapping[str, Any] | None = None,
) -> None:
    """Raise ``AutomationRefusal`` if ``delivery_config.methods`` conflicts with Dalton notify."""
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

    if automation_has_validated_report_profile(automation):
        raise AutomationRefusal(
            REPORT_PROFILE_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )

    if (
        not is_excluded_automation_for_notify(automation)
        and classify_automation_for_notify(automation) is not None
    ):
        raise AutomationRefusal(
            BUILTIN_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )

    auto_id = str(automation.get("automation_id") or "").strip()
    if not auto_id:
        return

    if isinstance(model_pref, Mapping) and DALTON_NOTIFY_ROUTES_KEY in model_pref:
        raw_routes = model_pref.get(DALTON_NOTIFY_ROUTES_KEY)
        routes: Mapping[str, Any] = raw_routes if isinstance(raw_routes, Mapping) else {}
    else:
        routes = await resolve_dalton_notify_routes(user_id)

    if has_active_dalton_notify_route(automation, routes):
        raise AutomationRefusal(
            NOTIFY_ROUTE_DELIVERY_CONFLICT_MSG,
            field="delivery_config",
        )
