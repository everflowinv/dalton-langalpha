# ruff: noqa: BLE001
"""Chief of Staff tool (`dalton_coverage`) for managing per-user coverage rosters and rule groups."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.coverage import (
    CoverageDataSourceError,
    add_coverage_group,
    add_user_coverage_tickers,
    list_user_coverage_summary,
    preview_coverage_group,
    remove_coverage_group,
    remove_user_coverage_tickers,
    set_user_coverage_tier,
)
from src.dalton.database import coverage as coverage_db

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
    }
)


def _normalize_tickers_arg(tickers: list[str] | str | None) -> list[str]:
    if tickers is None:
        return []
    if isinstance(tickers, str):
        raw = [part.strip() for part in tickers.replace(";", ",").split(",")]
        return [t for t in raw if t]
    if isinstance(tickers, (list, tuple, set)):
        out: list[str] = []
        for item in tickers:
            if isinstance(item, str):
                for sub in item.replace(";", ",").split(","):
                    if sub.strip():
                        out.append(sub.strip())
        return out
    return []


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
        config: RunnableConfig = None,
    ) -> str:
        """Inspect or modify the current user's stock coverage roster, dynamic rule groups, and earnings calendar watch pool.
        When the user names specific stocks, call `action="add"` directly; when the user describes a category of stocks (e.g. "美股半导体大票"), call `action="preview_group"` first, show the resolved list for confirmation, and call `action="add_group"` only after the user confirms.

        Args:
            action: `"list"`, `"add"`, `"remove"`, `"set_tier"`, `"preview_group"`, `"add_group"`, or `"remove_group"`.
            tickers: Ticker symbol(s) for `"add"`, `"remove"`, or `"set_tier"` (e.g. `["NVDA", "TSM"]` or `"0700.HK, 9988.HK"`).
            tier: Target tier (`"core"` or `"watch"`, default `"watch"`) for `"add"` or `"set_tier"`.
            label: User's natural description of a stock group for `"preview_group"` or `"add_group"` (e.g. `"美股半导体大票"`).
            criteria: Screener filter dict for `"preview_group"` or `"add_group"`. Keys: `country`, `exchange`, `sector`, `industry`, `market_cap_min`, `market_cap_max`, `limit`, `include` (extra tickers), `exclude` (excluded tickers).
            default_tier: Tier (`"core"` or `"watch"`, default `"watch"`) assigned to stocks resolved by `"add_group"`.
            group_id: Rule group UUID (or group `label`) for `"remove_group"`.

        Returns:
            JSON object with the coverage roster, group resolution preview, or mutation summary (including unrecognized tickers, preserved workspaces, and whether `dalton-earnings-calendar` was auto-created).

        After any roster change (`add`, `remove`, `set_tier`, `add_group`, `remove_group`), ask the user whether to trigger an immediate `dalton-earnings-calendar` refresh via `dalton_system_jobs(action="run_now", key="dalton-earnings-calendar")`.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")

        act = str(action or "").strip().lower()
        if act not in _VALID_ACTIONS:
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": (
                        f"不支持的 action `{action}`，可选值："
                        "list, add, remove, set_tier, preview_group, add_group, remove_group。"
                    ),
                },
                ensure_ascii=False,
            )

        try:
            if act == "list":
                res = await list_user_coverage_summary(user_id)
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "add":
                norm_tickers = _normalize_tickers_arg(tickers)
                if not norm_tickers:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_tickers",
                            "message": "执行 add 时必须提供 tickers。",
                        },
                        ensure_ascii=False,
                    )
                res = await add_user_coverage_tickers(
                    user_id,
                    norm_tickers,
                    tier=tier or "watch",
                    source="chat",
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "remove":
                norm_tickers = _normalize_tickers_arg(tickers)
                if not norm_tickers:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_tickers",
                            "message": "执行 remove 时必须提供 tickers。",
                        },
                        ensure_ascii=False,
                    )
                res = await remove_user_coverage_tickers(user_id, norm_tickers)
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "set_tier":
                norm_tickers = _normalize_tickers_arg(tickers)
                if not norm_tickers:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_tickers",
                            "message": "执行 set_tier 时必须提供 tickers。",
                        },
                        ensure_ascii=False,
                    )
                res = await set_user_coverage_tier(
                    user_id,
                    norm_tickers,
                    tier=tier or "watch",
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "preview_group":
                eff_label = str(label or "").strip() or "未命名规则组"
                res = await preview_coverage_group(
                    label=eff_label,
                    criteria=criteria or {},
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            if act == "add_group":
                eff_label = str(label or "").strip()
                if not eff_label:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "missing_label",
                            "message": "执行 add_group 时必须提供 label。",
                        },
                        ensure_ascii=False,
                    )
                eff_tier = str(default_tier or tier or "watch").strip().lower()
                res = await add_coverage_group(
                    user_id,
                    label=eff_label,
                    criteria=criteria or {},
                    default_tier=eff_tier,
                )
                return json.dumps(res, ensure_ascii=False, default=str)

            # act == "remove_group"
            target_gid = str(group_id or "").strip()
            if not target_gid and label:
                target_gid = str(label).strip()
            if not target_gid:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "missing_group_id",
                        "message": "执行 remove_group 时必须提供 group_id（或 label）。",
                    },
                    ensure_ascii=False,
                )
            # Allow matching by label if group_id is not a direct UUID match
            groups = await coverage_db.list_coverage_groups(user_id)
            matched_id = None
            for g in groups:
                if str(g.get("group_id") or "") == target_gid:
                    matched_id = str(g["group_id"])
                    break
            if matched_id is None:
                for g in groups:
                    if str(g.get("label") or "").strip().lower() == target_gid.lower():
                        matched_id = str(g["group_id"])
                        break
            res = await remove_coverage_group(user_id, matched_id or target_gid)
            return json.dumps(res, ensure_ascii=False, default=str)
        except CoverageDataSourceError as exc:
            logger.warning("[DALTON_COVERAGE_TOOL] action=%s data source error: %s", act, exc)
            return json.dumps(exc.to_dict(), ensure_ascii=False, default=str)
        except Exception as exc:
            logger.warning("[DALTON_COVERAGE_TOOL] action=%s failed: %s", act, exc)
            return json.dumps(
                {
                    "ok": False,
                    "error": "coverage_tool_error",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )

    return [dalton_coverage]
