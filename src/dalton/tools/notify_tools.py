"""Direct LangChain tool `dalton_notify_settings` for automation Feishu/Discord push configuration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.llm.model_prefs import (
    DALTON_NOTIFY_OVERRIDES_KEY,
    DALTON_NOTIFY_ROUTES_KEY,
    DALTON_NOTIFY_TARGET_LABELS_KEY,
    DALTON_NOTIFY_TARGETS_KEY,
    add_dalton_personal_notify_target,
    apply_dalton_notify_preference_patch,
    is_dalton_chat_target,
    is_valid_dalton_personal_target,
    normalize_dalton_notify_label,
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_routes,
    resolve_dalton_notify_target_labels,
    resolve_dalton_notify_targets,
)
from src.dalton.notify.dispatch import (
    classify_automation_for_notify,
    is_excluded_automation_for_notify,
    resolve_automation_notify_state,
    resolve_notify_route_targets,
    send_dalton_notify_test,
)
from src.dalton.notify.groups import FeishuGroupsUnavailable, discover_feishu_notify_groups
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.llms.preferences import TuningError
from src.server.database import automation as auto_db
from src.tools.secretary._commands import (
    InjectedToolCallId,
    decline_command,
    error_command,
    hitl_confirm,
    success_command,
)

logger = logging.getLogger(__name__)

NotifyActionLiteral = Literal["list", "set_route", "add_personal_target", "test"]

MASTER_SWITCH_OFF_NOTE = "你的推送总开关目前是关的，在设置里打开后才会推送"
NOTIFY_CONFIRM_CHANGED_ERROR = (
    "确认期间任务或推送目标发生变化（或确认校验未通过），已取消本次操作。"
)
NOTIFY_CONFIRM_TARGET_CHANGED_ERROR = (
    "确认校验未通过，已取消添加个人推送目标，请重新发起。"
)


def _compute_confirm_token(
    *,
    action: str,
    user_id: str,
    automation_id: str,
    route_key: str,
    targets: Sequence[str],
) -> str:
    """Compute a deterministic SHA-256 token over normalized confirmation fields."""
    canonical_payload = {
        "action": str(action or "").strip(),
        "user_id": str(user_id or "").strip(),
        "automation_id": str(automation_id or "").strip(),
        "route_key": str(route_key or "").strip(),
        "targets": sorted(str(t).strip() for t in targets if str(t).strip()),
    }
    encoded = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_confirm_token(response: Any) -> str | None:
    """Extract ``confirm_token`` from ``decisions[0]`` of a ``hitl_confirm`` response."""
    if not isinstance(response, Mapping):
        return None
    decisions = response.get("decisions")
    if (
        not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
        or not decisions
    ):
        return None
    first = decisions[0]
    if not isinstance(first, Mapping):
        return None
    raw_token = first.get("confirm_token")
    if isinstance(raw_token, str) and raw_token.strip():
        return raw_token.strip()
    return None


def _resolve_caller_user_id(config: RunnableConfig | None = None) -> str | None:
    """Resolve ``user_id`` from ``RunnableConfig``."""
    user_id, _, _, _ = _extract_run_identity(config)
    uid = (user_id or "").strip()
    return uid or None


def _format_candidate_automation(row: Mapping[str, Any]) -> dict[str, str]:
    created_raw = row.get("created_at")
    if hasattr(created_raw, "isoformat"):
        created_str = str(created_raw.isoformat())
    else:
        created_str = str(created_raw or "")
    return {
        "name": str(row.get("name") or "").strip(),
        "automation_id": str(row.get("automation_id") or "").strip(),
        "created_at": created_str,
    }


def _ambiguous_automation_error(
    automation_ref: str,
    candidates: list[dict[str, Any]],
    tool_call_id: str,
) -> Command:
    items = [_format_candidate_automation(row) for row in candidates]
    details = "；".join(
        f"{c['name']}（任务 ID automation_id={c['automation_id']}，创建时间={c['created_at']}）"
        for c in items
    )
    return error_command(
        f"定时任务名称 {automation_ref!r} 匹配到多个同名任务，请指定任务 ID（automation_id）：{details}",
        tool_call_id,
        candidates=items,
    )


def _match_user_automation(
    automations: list[dict[str, Any]],
    automation_ref: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Find an automation owned by the user by ``automation_id`` or display name.

    Returns ``(matched_row, ambiguous_candidates)``.
    """
    cleaned = automation_ref.strip()
    if not cleaned:
        return None, []
    candidate_id = (
        cleaned.removeprefix("automation:").strip()
        if cleaned.startswith("automation:")
        else cleaned
    )
    if candidate_id:
        id_matches = [
            row
            for row in automations
            if str(row.get("automation_id") or "").strip() == candidate_id
        ]
        if len(id_matches) == 1:
            return id_matches[0], []
    exact_matches = [
        row for row in automations if str(row.get("name") or "").strip() == cleaned
    ]
    if len(exact_matches) == 1:
        return exact_matches[0], []
    if len(exact_matches) > 1:
        return None, exact_matches
    lowered = cleaned.lower()
    ci_matches = [
        row
        for row in automations
        if str(row.get("name") or "").strip().lower() == lowered
    ]
    if len(ci_matches) == 1:
        return ci_matches[0], []
    if len(ci_matches) > 1:
        return None, ci_matches
    return None, []


def _automation_route_key(automation: Mapping[str, Any]) -> str | None:
    """Return the route/override storage key for ``automation``.

    Inherent categories keep their display name; custom automations use
    ``f"automation:{automation_id}"``.
    """
    if classify_automation_for_notify(automation) is not None:
        name = str(automation.get("name") or "").strip()
        return name or None
    auto_id = str(automation.get("automation_id") or "").strip()
    return f"automation:{auto_id}" if auto_id else None


def _format_target_display(
    target: str,
    personal_labels: Mapping[str, str],
    groups_by_chat_id: Mapping[str, str],
) -> str:
    """Format a canonical target with its human-readable label or group name."""
    if is_dalton_chat_target(target):
        chat_id = target.split(":", 2)[-1]
        group_name = groups_by_chat_id.get(chat_id) or ""
        return f"{group_name} ({target})" if group_name else target
    label = personal_labels.get(target) or ""
    return f"{label} ({target})" if label else target


def _format_target_card_label(
    target: str,
    personal_labels: Mapping[str, str],
    groups_by_chat_id: Mapping[str, str],
) -> str:
    """Format a canonical target showing both display name and canonical ID/chat_id for the approval card."""
    if is_dalton_chat_target(target):
        chat_id = target.split(":", 2)[-1]
        group_name = groups_by_chat_id.get(chat_id) or ""
        if group_name and group_name != chat_id:
            return f"{group_name}（群，{chat_id}）"
        return f"{chat_id}（群）"
    label = personal_labels.get(target) or ""
    if label and label != target:
        return f"{label}（个人，{target}）"
    return f"{target}（个人）"


async def _handle_list(user_id: str, tool_call_id: str) -> Command:
    enabled = await resolve_dalton_notify_enabled(user_id)
    personal_targets = await resolve_dalton_notify_targets(user_id)
    personal_labels = await resolve_dalton_notify_target_labels(user_id)
    overrides = await resolve_dalton_notify_overrides(user_id)
    routes = await resolve_dalton_notify_routes(user_id)
    automations = await auto_db.list_all_automations(user_id)

    groups_available = True
    groups_note: str | None = None
    raw_groups: list[dict[str, str]] = []
    try:
        raw_groups = await discover_feishu_notify_groups()
    except FeishuGroupsUnavailable:
        groups_available = False
        groups_note = "飞书群列表暂时不可用，请稍后再试"
    except Exception:
        groups_available = False
        groups_note = "飞书群列表暂时不可用，请稍后再试"

    groups_by_chat_id = {g["chat_id"]: g["name"] for g in raw_groups}
    groups_payload = [
        {
            "chat_id": g["chat_id"],
            "target": f"feishu:chat:{g['chat_id']}",
            "name": g["name"],
        }
        for g in raw_groups
    ]
    personal_payload = [
        {
            "target": t,
            "label": personal_labels.get(t) or t,
        }
        for t in personal_targets
    ]

    automation_items: list[dict[str, Any]] = []
    for row in automations:
        name = str(row.get("name") or "").strip()
        auto_id = str(row.get("automation_id") or "").strip()
        route_key = _automation_route_key(row)
        excluded = is_excluded_automation_for_notify(row)
        category, eff_enabled, source = resolve_automation_notify_state(
            row, overrides, routes
        )
        routed = resolve_notify_route_targets(
            row,
            routes,
            personal_targets,
            include_chat_targets=True,
        )
        if excluded or category is None:
            effective_targets: list[str] = []
        elif category == "custom":
            effective_targets = routed or []
        else:
            effective_targets = personal_targets if routed is None else routed
        automation_items.append(
            {
                "name": name,
                "automation_id": auto_id,
                "route_key": route_key,
                "status": str(row.get("status") or ""),
                "category": category,
                "excluded": excluded,
                "notify_enabled": bool(enabled and eff_enabled and not excluded),
                "task_switch_enabled": eff_enabled,
                "switch_source": source,
                "route_configured": routed is not None,
                "route_targets": routed,
                "effective_targets": [
                    {
                        "target": t,
                        "display": _format_target_display(t, personal_labels, groups_by_chat_id),
                    }
                    for t in effective_targets
                ],
            }
        )

    payload: dict[str, Any] = {
        "success": True,
        "action": "list",
        "notify_enabled": enabled,
        "personal_targets": personal_payload,
        "groups_available": groups_available,
        "groups": groups_payload,
        "routes": routes,
        "automations": automation_items,
    }
    if not enabled:
        payload["note"] = MASTER_SWITCH_OFF_NOTE
    if groups_note is not None:
        payload["groups_note"] = groups_note
    return success_command(payload, tool_call_id)


async def _resolve_requested_targets(
    raw_targets: list[str],
    *,
    personal_targets: list[str],
    personal_labels: Mapping[str, str],
) -> tuple[list[str] | None, dict[str, str], str | None]:
    """Resolve user-provided target strings (canonical IDs, chat_ids, or display names) to canonical targets."""
    if not raw_targets:
        return [], {}, None

    personal_set = set(personal_targets)
    groups_by_chat_id: dict[str, str] | None = None

    async def _ensure_groups() -> tuple[dict[str, str] | None, str | None]:
        nonlocal groups_by_chat_id
        if groups_by_chat_id is not None:
            return groups_by_chat_id, None
        try:
            discovered = await discover_feishu_notify_groups()
        except FeishuGroupsUnavailable:
            return None, "飞书群列表暂时不可用，请稍后再试"
        groups_by_chat_id = {g["chat_id"]: g["name"] for g in discovered}
        return groups_by_chat_id, None

    resolved: list[str] = []
    seen: set[str] = set()

    for raw in raw_targets:
        item = str(raw or "").strip()
        if not item:
            return None, {}, "推送目标不能为空字符串"

        # 1. Canonical personal target (feishu:user:ou_... or discord:user:...)
        if is_valid_dalton_personal_target(item):
            if item not in personal_set:
                return (
                    None,
                    {},
                    f"个人推送目标 {item!r} 尚未添加，请先添加个人推送目标（action='add_personal_target'）",
                )
            if item not in seen:
                seen.add(item)
                resolved.append(item)
            continue

        # 2. Canonical chat target (feishu:chat:oc_...) or bare chat_id (oc_...)
        candidate_chat = item if item.startswith("feishu:chat:") else f"feishu:chat:{item}"
        if is_dalton_chat_target(candidate_chat):
            g_map, err = await _ensure_groups()
            if err is not None or g_map is None:
                return None, {}, err or "飞书群列表暂时不可用"
            chat_id = candidate_chat.split(":", 2)[-1]
            if chat_id not in g_map:
                return (
                    None,
                    {},
                    f"群 {candidate_chat} 不在机器人已加入的群列表中（请先把机器人拉进群）",
                )
            if candidate_chat not in seen:
                seen.add(candidate_chat)
                resolved.append(candidate_chat)
            continue

        # 3. Match personal target by display label
        matched_personal = next(
            (
                t
                for t in personal_targets
                if (personal_labels.get(t) or "").strip() == item
                or (personal_labels.get(t) or "").strip().lower() == item.lower()
            ),
            None,
        )
        if matched_personal is not None:
            if matched_personal not in seen:
                seen.add(matched_personal)
                resolved.append(matched_personal)
            continue

        # 4. Match discovered Feishu group by group name
        g_map, err = await _ensure_groups()
        if err is not None or g_map is None:
            return None, {}, err or f"无法解析目标 {item!r}：飞书群列表暂时不可用"
        matched_chat_id = next(
            (
                cid
                for cid, gname in g_map.items()
                if gname.strip() == item or gname.strip().lower() == item.lower()
            ),
            None,
        )
        if matched_chat_id is not None:
            canonical_group = f"feishu:chat:{matched_chat_id}"
            if canonical_group not in seen:
                seen.add(canonical_group)
                resolved.append(canonical_group)
            continue

        return (
            None,
            g_map,
            f"目标 {item!r} 不在已配置的个人目标或机器人已加入的飞书群列表中",
        )

    return resolved, groups_by_chat_id or {}, None


async def _handle_set_route(
    user_id: str,
    automation: str | None,
    targets: list[str] | None,
    tool_call_id: str,
) -> Command:
    cleaned_auto = (automation or "").strip()
    if not cleaned_auto:
        return error_command(
            "设置定时任务推送（set_route）需要提供定时任务名称或任务 ID（automation / automation_id）",
            tool_call_id,
        )
    if targets is None or not isinstance(targets, list):
        return error_command(
            "设置定时任务推送（set_route）需要提供推送目标列表（targets；传空列表 [] 表示该任务不推送）",
            tool_call_id,
        )

    user_automations = await auto_db.list_all_automations(user_id)
    matched, ambiguous = _match_user_automation(user_automations, cleaned_auto)
    if ambiguous:
        return _ambiguous_automation_error(cleaned_auto, ambiguous, tool_call_id)
    if matched is None:
        return error_command(
            f"定时任务 {cleaned_auto!r} 不存在或不属于当前用户",
            tool_call_id,
        )
    canonical_name = str(matched.get("name") or cleaned_auto).strip()
    auto_id = str(matched.get("automation_id") or "").strip()
    if is_excluded_automation_for_notify(matched):
        return error_command(
            f"定时任务 {canonical_name!r} 是系统后台内部任务，不支持设置推送目标",
            tool_call_id,
        )
    route_key = _automation_route_key(matched)
    if not route_key:
        return error_command(
            f"定时任务 {canonical_name!r} 缺少有效的任务 ID（automation_id）",
            tool_call_id,
        )

    master_enabled = await resolve_dalton_notify_enabled(user_id)
    personal_targets = await resolve_dalton_notify_targets(user_id)
    personal_labels = await resolve_dalton_notify_target_labels(user_id)

    resolved_targets, groups_by_chat_id, resolve_err = (
        await _resolve_requested_targets(
            targets,
            personal_targets=personal_targets,
            personal_labels=personal_labels,
        )
    )
    if resolve_err is not None or resolved_targets is None:
        return error_command(resolve_err or "推送目标校验失败", tool_call_id)
    initial_targets = list(resolved_targets)

    confirm_token = _compute_confirm_token(
        action="set_route",
        user_id=user_id,
        automation_id=auto_id,
        route_key=route_key,
        targets=initial_targets,
    )

    target_displays = [
        _format_target_display(t, personal_labels, groups_by_chat_id)
        for t in initial_targets
    ]
    card_targets = [
        _format_target_card_label(t, personal_labels, groups_by_chat_id)
        for t in initial_targets
    ]
    if target_displays:
        summary = f"{canonical_name} → {'、'.join(target_displays)}"
    else:
        summary = f"{canonical_name} → 不推送"

    card_args: dict[str, Any] = {
        "任务": canonical_name,
        "推送到": card_targets if card_targets else "不推送",
    }
    description = f"确认设置定时任务推送：{summary}"
    if not master_enabled:
        card_args["提示"] = MASTER_SWITCH_OFF_NOTE
        description = f"{description}（{MASTER_SWITCH_OFF_NOTE}）"

    approved, hitl_response = hitl_confirm(
        "dalton_notify_set_route",
        {
            "name": "设置定时任务推送",
            "args": card_args,
            "action": "set_route",
            "automation": canonical_name,
            "automation_id": auto_id,
            "route_key": route_key,
            "targets": initial_targets,
            "target_displays": target_displays,
            "summary": summary,
            "description": description,
            "confirm_token": confirm_token,
        },
    )
    if not approved:
        return decline_command(
            f"用户取消了定时任务 {canonical_name} 的推送设置。",
            tool_call_id,
        )

    returned_token = _extract_confirm_token(hitl_response)
    user_automations_now = await auto_db.list_all_automations(user_id)
    matched_now, ambiguous_now = _match_user_automation(
        user_automations_now, cleaned_auto
    )
    canonical_name_now = (
        str(matched_now.get("name") or cleaned_auto).strip()
        if matched_now is not None
        else ""
    )
    auto_id_now = (
        str(matched_now.get("automation_id") or "").strip()
        if matched_now is not None
        else ""
    )
    route_key_now = (
        (_automation_route_key(matched_now) or "")
        if matched_now is not None and not is_excluded_automation_for_notify(matched_now)
        else ""
    )

    personal_targets_now = await resolve_dalton_notify_targets(user_id)
    personal_labels_now = await resolve_dalton_notify_target_labels(user_id)
    current_targets, groups_now, current_err = await _resolve_requested_targets(
        targets,
        personal_targets=personal_targets_now,
        personal_labels=personal_labels_now,
    )
    current_token: str | None = None
    if (
        not ambiguous_now
        and matched_now is not None
        and route_key_now
        and current_err is None
        and current_targets is not None
    ):
        current_token = _compute_confirm_token(
            action="set_route",
            user_id=user_id,
            automation_id=auto_id_now,
            route_key=route_key_now,
            targets=current_targets,
        )

    if (
        not returned_token
        or current_token is None
        or returned_token != current_token
        or confirm_token != current_token
    ):
        new_list = list(current_targets) if isinstance(current_targets, list) else []
        targets_text = (
            "、".join(new_list)
            if new_list
            else ("解析失败" if current_err else "不推送")
        )
        new_task = (
            {
                "name": canonical_name_now,
                "automation_id": auto_id_now,
                "route_key": route_key_now,
            }
            if matched_now is not None
            else None
        )
        return error_command(
            (
                f"{NOTIFY_CONFIRM_CHANGED_ERROR}"
                f"当前状态：任务 {canonical_name_now or '不存在'}（任务 ID：{auto_id_now or '无'}）"
                f"→ 目标：{targets_text}。如需继续请重新发起。"
            ),
            tool_call_id,
            current_automation=new_task,
            current_targets=new_list,
        )

    final_targets = list(current_targets)
    final_displays = [
        _format_target_display(t, personal_labels_now, groups_now)
        for t in final_targets
    ]
    if final_displays:
        final_summary = f"{canonical_name} → {'、'.join(final_displays)}"
    else:
        final_summary = f"{canonical_name} → 不推送"

    patch: dict[str, Any] = {
        DALTON_NOTIFY_ROUTES_KEY: {route_key: final_targets},
    }
    if final_targets:
        patch[DALTON_NOTIFY_OVERRIDES_KEY] = {route_key: True}

    try:
        await apply_dalton_notify_preference_patch(user_id, patch)
    except TuningError as exc:
        return error_command(str(exc), tool_call_id)
    except HTTPException as exc:
        return error_command(str(exc.detail), tool_call_id)

    result_payload: dict[str, Any] = {
        "success": True,
        "action": "set_route",
        "automation": canonical_name,
        "automation_id": auto_id,
        "route_key": route_key,
        "targets": final_targets,
        "target_displays": final_displays,
        "notify_enabled": master_enabled,
        "summary": (
            f"{final_summary}（{MASTER_SWITCH_OFF_NOTE}）"
            if not master_enabled
            else final_summary
        ),
    }
    if not master_enabled:
        result_payload["note"] = MASTER_SWITCH_OFF_NOTE
    return success_command(result_payload, tool_call_id)


async def _handle_add_personal_target(
    user_id: str,
    target: str | None,
    label: str | None,
    tool_call_id: str,
) -> Command:
    cleaned_target = (target or "").strip()
    if not cleaned_target:
        return error_command(
            "添加个人推送目标（add_personal_target）需要提供个人目标地址（target，如 feishu:user:ou_... 或 discord:user:...）",
            tool_call_id,
        )
    if is_dalton_chat_target(cleaned_target):
        return error_command(
            "飞书群（feishu:chat:oc_...）不能添加为个人推送目标，请用设置定时任务推送（action='set_route'）按任务配置",
            tool_call_id,
        )
    if not is_valid_dalton_personal_target(cleaned_target):
        return error_command(
            f"个人推送目标格式无效：{cleaned_target!r}，仅支持飞书个人目标 feishu:user:<飞书用户 ID（ou_ 开头）> 或 Discord 个人目标 discord:user:<数字用户 ID>",
            tool_call_id,
        )

    cleaned_label = normalize_dalton_notify_label(label or "")
    display = f"{cleaned_label} ({cleaned_target})" if cleaned_label else cleaned_target
    card_args: dict[str, Any] = {"目标": cleaned_target}
    if cleaned_label:
        card_args["名称"] = cleaned_label

    confirm_token = _compute_confirm_token(
        action="add_personal_target",
        user_id=user_id,
        automation_id="",
        route_key="",
        targets=[cleaned_target],
    )

    approved, hitl_response = hitl_confirm(
        "dalton_notify_add_personal_target",
        {
            "name": "添加个人推送目标",
            "args": card_args,
            "action": "add_personal_target",
            "target": cleaned_target,
            "label": cleaned_label or None,
            "summary": f"添加个人推送目标：{display}",
            "description": f"确认添加个人推送目标：{display}",
            "confirm_token": confirm_token,
        },
    )
    if not approved:
        return decline_command("用户取消了添加个人推送目标。", tool_call_id)

    returned_token = _extract_confirm_token(hitl_response)
    current_token = _compute_confirm_token(
        action="add_personal_target",
        user_id=user_id,
        automation_id="",
        route_key="",
        targets=[cleaned_target],
    )
    if (
        not returned_token
        or returned_token != current_token
        or confirm_token != current_token
    ):
        return error_command(
            NOTIFY_CONFIRM_TARGET_CHANGED_ERROR,
            tool_call_id,
            current_automation=None,
            current_targets=[cleaned_target],
        )

    try:
        updated = await add_dalton_personal_notify_target(
            user_id, cleaned_target, cleaned_label or None
        )
    except TuningError as exc:
        return error_command(str(exc), tool_call_id)
    except HTTPException as exc:
        return error_command(str(exc.detail), tool_call_id)

    updated_model_pref = dict((updated or {}).get("model_preference") or {})
    final_targets = updated_model_pref.get(DALTON_NOTIFY_TARGETS_KEY)
    if not isinstance(final_targets, list):
        final_targets = await resolve_dalton_notify_targets(user_id)
    final_labels = dict(await resolve_dalton_notify_target_labels(user_id))
    patched_labels = updated_model_pref.get(DALTON_NOTIFY_TARGET_LABELS_KEY)
    if isinstance(patched_labels, Mapping):
        for k, v in patched_labels.items():
            if isinstance(v, str) and v:
                final_labels[k] = v

    return success_command(
        {
            "success": True,
            "action": "add_personal_target",
            "target": cleaned_target,
            "label": cleaned_label or None,
            "personal_targets": final_targets,
            "target_labels": final_labels,
        },
        tool_call_id,
    )


async def _handle_test(
    user_id: str,
    automation: str | None,
    tool_call_id: str,
) -> Command:
    cleaned_auto = (automation or "").strip()
    if not cleaned_auto:
        return error_command(
            "发送测试推送（test）需要提供定时任务名称或任务 ID（automation / automation_id）",
            tool_call_id,
        )

    user_automations = await auto_db.list_all_automations(user_id)
    matched, ambiguous = _match_user_automation(user_automations, cleaned_auto)
    if ambiguous:
        return _ambiguous_automation_error(cleaned_auto, ambiguous, tool_call_id)
    if matched is None:
        return error_command(
            f"定时任务 {cleaned_auto!r} 不存在或不属于当前用户",
            tool_call_id,
        )
    canonical_name = str(matched.get("name") or cleaned_auto).strip()
    auto_id = str(matched.get("automation_id") or "").strip()
    route_key = _automation_route_key(matched) or ""

    personal_targets = await resolve_dalton_notify_targets(user_id)
    personal_labels = await resolve_dalton_notify_target_labels(user_id)
    routes = await resolve_dalton_notify_routes(user_id)

    routed = resolve_notify_route_targets(
        matched,
        routes,
        personal_targets,
        include_chat_targets=True,
    )
    if not routed:
        return error_command(
            "该任务尚未单独设置推送目标（或当前已设为不推送），请先设置定时任务推送（action='set_route'）后再发送测试推送",
            tool_call_id,
        )
    target_list = list(routed)

    confirm_token = _compute_confirm_token(
        action="test",
        user_id=user_id,
        automation_id=auto_id,
        route_key=route_key,
        targets=target_list,
    )

    groups_by_chat_id: dict[str, str] = {}
    if any(is_dalton_chat_target(t) for t in target_list):
        try:
            groups = await discover_feishu_notify_groups()
            groups_by_chat_id = {g["chat_id"]: g["name"] for g in groups}
        except FeishuGroupsUnavailable:
            groups_by_chat_id = {}

    target_displays = [
        _format_target_display(t, personal_labels, groups_by_chat_id)
        for t in target_list
    ]
    card_targets = [
        _format_target_card_label(t, personal_labels, groups_by_chat_id)
        for t in target_list
    ]
    summary = f"{canonical_name} → {'、'.join(target_displays)}"

    approved, hitl_response = hitl_confirm(
        "dalton_notify_test",
        {
            "name": "发送测试推送",
            "args": {
                "任务": canonical_name,
                "推送到": card_targets,
            },
            "action": "test",
            "automation": canonical_name,
            "automation_id": auto_id,
            "targets": target_list,
            "target_displays": target_displays,
            "summary": summary,
            "description": f"确认发送测试推送：{summary}",
            "confirm_token": confirm_token,
        },
    )
    if not approved:
        return decline_command(
            f"用户取消了定时任务 {canonical_name} 的测试推送。",
            tool_call_id,
        )

    returned_token = _extract_confirm_token(hitl_response)
    user_automations_now = await auto_db.list_all_automations(user_id)
    matched_now, ambiguous_now = _match_user_automation(
        user_automations_now, cleaned_auto
    )
    canonical_name_now = (
        str(matched_now.get("name") or cleaned_auto).strip()
        if matched_now is not None
        else ""
    )
    auto_id_now = (
        str(matched_now.get("automation_id") or "").strip()
        if matched_now is not None
        else ""
    )
    route_key_now = (
        (_automation_route_key(matched_now) or "")
        if matched_now is not None
        else ""
    )

    personal_targets_now = await resolve_dalton_notify_targets(user_id)
    personal_labels_now = await resolve_dalton_notify_target_labels(user_id)
    routes_now = await resolve_dalton_notify_routes(user_id)
    routed_now = (
        resolve_notify_route_targets(
            matched_now,
            routes_now,
            personal_targets_now,
            include_chat_targets=True,
        )
        if matched_now is not None and not ambiguous_now
        else None
    )
    current_targets = list(routed_now) if routed_now else []
    current_token: str | None = None
    if (
        not ambiguous_now
        and matched_now is not None
        and route_key_now
        and current_targets
    ):
        current_token = _compute_confirm_token(
            action="test",
            user_id=user_id,
            automation_id=auto_id_now,
            route_key=route_key_now,
            targets=current_targets,
        )

    if (
        not returned_token
        or current_token is None
        or returned_token != current_token
        or confirm_token != current_token
    ):
        new_list = list(current_targets)
        targets_text = "、".join(new_list) if new_list else "不推送"
        new_task = (
            {
                "name": canonical_name_now,
                "automation_id": auto_id_now,
                "route_key": route_key_now,
            }
            if matched_now is not None
            else None
        )
        return error_command(
            (
                f"{NOTIFY_CONFIRM_CHANGED_ERROR}"
                f"当前状态：任务 {canonical_name_now or '不存在'}（任务 ID：{auto_id_now or '无'}）"
                f"→ 目标：{targets_text}。如需继续请重新发起。"
            ),
            tool_call_id,
            current_automation=new_task,
            current_targets=new_list,
        )

    groups_now: dict[str, str] = {}
    if any(is_dalton_chat_target(t) for t in current_targets):
        try:
            groups = await discover_feishu_notify_groups()
            groups_now = {g["chat_id"]: g["name"] for g in groups}
        except FeishuGroupsUnavailable:
            groups_now = {}
    final_displays = [
        _format_target_display(t, personal_labels_now, groups_now)
        for t in current_targets
    ]

    results: list[dict[str, Any]] = []
    for tgt, disp in zip(current_targets, final_displays, strict=True):
        try:
            res = await send_dalton_notify_test(user_id, tgt)
            results.append(
                {
                    "target": tgt,
                    "display": disp,
                    "ok": bool(res.get("ok")),
                    "detail": res.get("detail"),
                    "error_code": res.get("error_code"),
                }
            )
        except HTTPException as exc:
            results.append(
                {
                    "target": tgt,
                    "display": disp,
                    "ok": False,
                    "detail": str(exc.detail),
                    "error_code": f"http_{exc.status_code}",
                }
            )

    all_ok = all(item.get("ok") is True for item in results)
    return success_command(
        {
            "success": all_ok,
            "action": "test",
            "automation": canonical_name,
            "automation_id": auto_id,
            "results": results,
        },
        tool_call_id,
    )


def create_notify_tools() -> list[BaseTool]:
    """Build `[dalton_notify_settings]` for internal and admin members."""

    @tool("dalton_notify_settings")
    async def dalton_notify_settings(
        action: NotifyActionLiteral,
        automation: str | None = None,
        targets: list[str] | None = None,
        target: str | None = None,
        label: str | None = None,
        *,
        config: RunnableConfig = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Inspect, configure, or test Feishu group and personal push notifications for scheduled automations.

        Completion cards are generated and pushed automatically after each run; mutating actions and `"test"` ask for one-time user confirmation. Call `action="list"` first before `"set_route"` or `"test"` to see personal targets, joined Feishu groups, and each automation's route/switch state (automations with `excluded=true` cannot be routed).

        Args:
            action: `"list"`, `"set_route"`, `"add_personal_target"`, or `"test"` (sends a test card to each target in the automation's configured `route_targets`, which requires a non-empty route set via `"set_route"`).
            automation: Scheduled automation name or `automation_id` owned by the user (required for `"set_route"` and `"test"`).
            targets: Personal or group targets for `"set_route"` — display label, group name, group `chat_id` (`oc_...`), or canonical ID (`feishu:chat:oc_...`, `feishu:user:ou_...`, `discord:user:...`); `[]` disables push for that task.
            target: Personal target ID (`feishu:user:ou_...` or `discord:user:...`) for `"add_personal_target"`.
            label: Optional display label for `"add_personal_target"`.

        Returns:
            JSON with `notify_enabled` (master switch), `personal_targets`, joined Feishu `groups`, and per-automation routes/switches, or the confirmed update/test result.
        """
        user_id = _resolve_caller_user_id(config)
        if not user_id:
            return error_command("Authentication required: user_id not found in config", tool_call_id)
        if not await is_internal_or_admin(user_id):
            return error_command(
                "当前账号为外部成员，不支持使用定时任务推送配置工具",
                tool_call_id,
            )

        if action == "list":
            return await _handle_list(user_id, tool_call_id)
        if action == "set_route":
            return await _handle_set_route(user_id, automation, targets, tool_call_id)
        if action == "add_personal_target":
            return await _handle_add_personal_target(user_id, target, label, tool_call_id)
        if action == "test":
            return await _handle_test(user_id, automation, tool_call_id)
        return error_command(f"Unsupported action: {action!r}", tool_call_id)

    return [dalton_notify_settings]


__all__ = ["create_notify_tools"]
