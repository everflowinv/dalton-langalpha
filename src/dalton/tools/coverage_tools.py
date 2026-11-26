# ruff: noqa: BLE001
"""Chief of Staff tool (`dalton_coverage`) for managing per-user three-tier watchlist and rule groups."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from src.dalton.company_identity import is_company_system_user
from src.dalton.coverage import (
    CoverageDataSourceError,
    list_user_coverage_summary,
    preview_coverage_group,
)
from src.dalton.watchlist.policies import get_user_tier_policies
from src.dalton.watchlist.service import (
    WatchlistMutationService,
    WatchlistScopeChangedError,
    normalize_tickers_input,
)
from src.tools.secretary._commands import (
    InjectedToolCallId,
    decline_command,
    error_command,
    hitl_confirm,
    success_command,
)
from src.tools.secretary.approvals import approval_snapshot

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset(
    {
        "list",
        "add",
        "remove",
        "set_tier",
        "preview_group",
        "add_group",
        "remove_group",
        "update_tags",
        "get_tier_policy",
    }
)


def _json_safe(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _normalize_tickers_arg(tickers: list[str] | str | None) -> list[str]:
    return normalize_tickers_input(tickers)


def _build_confirm_card_payload(
    snapshot: dict[str, Any],
    *,
    user_id: str,
    raw_action: str,
    tool_call_id: str,
) -> dict[str, Any]:
    """Build the HITL interrupt payload echoed to the web card and verified by checkpoint_handler."""
    return {
        "tool_call_id": tool_call_id,
        "snapshot_id": snapshot.get("snapshot_id"),
        "owner_user_id": str(snapshot.get("owner_user_id") or user_id),
        "raw_action": str(snapshot.get("raw_action") or raw_action),
        "name": str(snapshot.get("card_title") or "确认修改个人观察名单"),
        "description": str(snapshot.get("card_description") or ""),
        "expected_roster_hash": snapshot.get("expected_roster_hash"),
        "expected_policy_versions": dict(snapshot.get("expected_policy_versions") or {}),
        "target_tickers": list(snapshot.get("target_tickers") or []),
        "target_tier": snapshot.get("target_tier"),
        "affected_tickers": list(snapshot.get("affected_tickers") or []),
        "affected_workspaces": list(snapshot.get("affected_workspaces") or []),
        "task_summary": dict(snapshot.get("task_summary") or {}),
        "preserve_workspace_notice": snapshot.get("preserve_workspace_notice"),
        "group_id": snapshot.get("group_id"),
        "group_label": snapshot.get("group_label"),
    }


def build_coverage_tools(*, role: str = "chief_of_staff") -> list[BaseTool]:
    """Return `[dalton_coverage]` for `chief_of_staff` only."""
    if role != "chief_of_staff":
        return []

    @tool("dalton_coverage")
    async def dalton_coverage(
        action: str,
        tickers: list[str] | str | None = None,
        tier: str = "watch",
        label: str | None = None,
        criteria: dict[str, Any] | None = None,
        default_tier: str | None = None,
        group_id: str | None = None,
        tags: list[str] | None = None,
        override_user_pause: bool = False,
        *,
        config: RunnableConfig = None,
        state: Annotated[dict[str, Any] | None, InjectedState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """查看或维护当前用户个人的三档股票观察名单（核心 core / 跟踪 watch / 雷达 radar）、动态规则组与各档默认监控策略。

        操作说明：
        - 查询名单（`list`）、预览规则组（`preview_group`）、查看各档策略（`get_tier_policy`）、更新标签（`update_tags`）以及点名新增不超过 5 只且不改变已有分档的股票（`add`）直接执行。
        - 批量新增超过 5 只股票、添加动态规则组（`add_group`）、调整股票分档（`set_tier` 或 `add` 命中已在其他分档的股票）、删除股票或规则组（`remove` / `remove_group`）会弹出确认卡片，由用户在网页端确认后按快照执行；若确认前名单或策略已变化，则中止并提示重新发起。
        - 用户按条件描述一类股票时（如「美股半导体大票」），先调用 `preview_group` 展示候选列表，再调用 `add_group`。

        Args:
            action: 可选 `"list"`、`"add"`、`"remove"`、`"set_tier"`、`"preview_group"`、`"add_group"`、`"remove_group"`、`"update_tags"` 或 `"get_tier_policy"`。
            tickers: 股票代码列表或逗号分隔字符串（如 `["NVDA", "TSM"]` 或 `"0700.HK, 9988.HK"`），用于 `add`、`remove`、`set_tier`、`update_tags`。
            tier: 目标分档，可选 `"core"`（核心）、`"watch"`（跟踪，默认）或 `"radar"`（雷达，仅观察与财报日历，默认不开启后台研究任务）。
            label: 规则组名称（用于 `preview_group`、`add_group`，也可用于 `remove_group` 按名称匹配）。
            criteria: 规则组筛选条件字典，支持 `country`、`exchange`、`sector`、`industry`、`market_cap_min`、`market_cap_max`、`limit`、`include`、`exclude`。
            default_tier: 规则组新纳入股票的默认分档（`"core"` / `"watch"` / `"radar"`，默认 `"watch"`）。
            group_id: 规则组 ID 或名称（用于 `remove_group`）。
            tags: 个人标签字符串列表（用于 `update_tags`）。
            override_user_pause: 调档开启任务时是否同时恢复用户手动暂停的任务，默认 `False`。

        Returns:
            包含名单摘要、规则组预览、各档策略或变更结果的 JSON 载荷。完成名单变更后，询问用户是否通过 `dalton_system_jobs(action="run_now", key="dalton-earnings-calendar")` 立即刷新个人财报日历。
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            return error_command(
                "user_id is required",
                tool_call_id,
                ok=False,
                code="missing_user_id",
            )
        if is_company_system_user(user_id):
            return error_command(
                "个人观察名单仅限用户本人读写。",
                tool_call_id,
                ok=False,
                code="company_system_forbidden",
            )

        act = str(action or "").strip().lower()
        if act not in _VALID_ACTIONS:
            return error_command(
                f"不支持的 action `{action}`，可选值："
                "list, add, remove, set_tier, preview_group, add_group, remove_group, update_tags, get_tier_policy。",
                tool_call_id,
                ok=False,
                code="invalid_action",
            )

        service = WatchlistMutationService()
        snap: dict[str, Any] | None = None

        try:
            if act == "list":
                res = await list_user_coverage_summary(user_id)
                return success_command(_json_safe({**res, "success": True}), tool_call_id)

            if act == "get_tier_policy":
                policies = await get_user_tier_policies(user_id)
                return success_command(
                    _json_safe(
                        {
                            "ok": True,
                            "success": True,
                            "user_id": user_id,
                            "policies": {t: p.to_dict() for t, p in policies.items()},
                        }
                    ),
                    tool_call_id,
                )

            if act == "preview_group":
                eff_label = str(label or "").strip() or "未命名规则组"
                res = await preview_coverage_group(
                    label=eff_label,
                    criteria=criteria or {},
                )
                return success_command(_json_safe({**res, "success": True}), tool_call_id)

            if act == "update_tags":
                norm_tickers = _normalize_tickers_arg(tickers)
                if not norm_tickers:
                    return error_command(
                        "执行 update_tags 时必须提供 tickers。",
                        tool_call_id,
                        ok=False,
                        code="missing_tickers",
                    )
                res = await service.execute_mutation(
                    user_id,
                    action="update_tags",
                    tickers=norm_tickers,
                    tags=tags or [],
                    source="chat",
                )
                return success_command(_json_safe({**res, "success": True}), tool_call_id)

            # Mutating actions: add, remove, set_tier, add_group, remove_group
            # Read persisted snapshot from StandingApprovalMiddleware if present;
            # never re-resolve on interrupt resume.
            snap = approval_snapshot(state, tool_call_id)
            if snap is None:
                # Validate basic required args cleanly before preview
                if act in ("add", "remove", "set_tier") and not _normalize_tickers_arg(tickers):
                    return error_command(
                        f"执行 {act} 时必须提供 tickers。",
                        tool_call_id,
                        ok=False,
                        code="missing_tickers",
                    )
                if act == "add_group" and not str(label or "").strip():
                    return error_command(
                        "执行 add_group 时必须提供 label。",
                        tool_call_id,
                        ok=False,
                        code="missing_label",
                    )
                if act == "remove_group" and not str(group_id or label or "").strip():
                    return error_command(
                        "执行 remove_group 时必须提供 group_id（或 label）。",
                        tool_call_id,
                        ok=False,
                        code="missing_group_id",
                    )
                snap = await service.preview_mutation(
                    user_id,
                    action=act,
                    tickers=tickers,
                    tier=tier,
                    label=label,
                    criteria=criteria,
                    default_tier=default_tier,
                    group_id=group_id,
                    override_user_pause=override_user_pause,
                    tool_call_id=tool_call_id,
                )

            if snap.get("precheck_error"):
                payload = snap.get("precheck_error_payload")
                if isinstance(payload, dict):
                    return error_command(
                        str(snap["precheck_error"]),
                        tool_call_id,
                        **payload,
                    )
                return error_command(
                    str(snap["precheck_error"]),
                    tool_call_id,
                    ok=False,
                    code=str(snap.get("precheck_error_code") or snap.get("error") or "precheck_failed"),
                )

            # Handle empty set_tier / remove (all requested tickers missing from roster)
            if act == "set_tier" and not (snap.get("target_tickers") or []):
                return success_command(
                    {
                        "ok": True,
                        "success": True,
                        "updated": [],
                        "updated_tickers": [],
                        "missing_tickers": list(snap.get("not_found") or []),
                        "not_found": list(snap.get("not_found") or []),
                        "tier": snap.get("target_tier") or tier or "watch",
                        "home_synced": False,
                    },
                    tool_call_id,
                )

            action_type = snap.get("action")
            if snap.get("requires_confirmation") and action_type:
                card_payload = _build_confirm_card_payload(
                    snap,
                    user_id=user_id,
                    raw_action=act,
                    tool_call_id=tool_call_id,
                )
                approved, response = hitl_confirm(str(action_type), card_payload)
                if isinstance(response, dict) and response.get("scope_changed"):
                    return error_command(
                        "情况已变化，请重新发起",
                        tool_call_id,
                        code="scope_changed",
                        message_status="error",
                        ok=False,
                        action_type=str(action_type),
                    )
                if not approved:
                    return decline_command(
                        "User declined personal watchlist update.",
                        tool_call_id,
                    )

            res = await service.execute_mutation(
                user_id,
                action=act,
                snapshot_id=snap.get("snapshot_id") if snap.get("requires_confirmation") else None,
                tickers=tickers if not snap.get("requires_confirmation") else None,
                tier=tier if not snap.get("requires_confirmation") else None,
                source="chat",
                label=label if not snap.get("requires_confirmation") else None,
                criteria=criteria if not snap.get("requires_confirmation") else None,
                default_tier=default_tier if not snap.get("requires_confirmation") else None,
                group_id=(snap.get("group_id") or group_id) if not snap.get("requires_confirmation") else None,
                override_user_pause=(
                    bool(snap.get("override_user_pause", override_user_pause))
                    if not snap.get("requires_confirmation")
                    else None
                ),
            )
            return success_command(
                _json_safe(
                    {
                        **res,
                        "success": True,
                        "action_type": action_type,
                        "tool_call_id": tool_call_id,
                    }
                ),
                tool_call_id,
            )
        except GraphBubbleUp:
            raise
        except WatchlistScopeChangedError:
            return error_command(
                "情况已变化，请重新发起",
                tool_call_id,
                code="scope_changed",
                message_status="error",
                ok=False,
                action_type=snap.get("action") if isinstance(snap, dict) else None,
            )
        except CoverageDataSourceError as exc:
            logger.warning("[DALTON_COVERAGE_TOOL] action=%s data source error: %s", act, exc)
            return error_command(str(exc), tool_call_id, **exc.to_dict())
        except Exception as exc:
            logger.warning("[DALTON_COVERAGE_TOOL] action=%s failed (%s)", act, type(exc).__name__)
            return error_command(
                str(exc),
                tool_call_id,
                ok=False,
                code="coverage_tool_error",
            )

    return [dalton_coverage]
