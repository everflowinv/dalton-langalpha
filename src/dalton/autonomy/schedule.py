"""Automation naming, templates, and idempotent scheduling helpers for Dalton (PK.15)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.dalton.settings import get_dalton_settings

DALTON_NAME_RE = re.compile(r"^dalton-[a-z0-9][a-z0-9._-]{1,62}$")


def validate_automation_name(name: str, ticker: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Validate that an automation name follows the `dalton-*` convention.

    If ``ticker`` is provided (analyst workspace), the name must start with
    ``dalton-<ticker_lower>-``.
    """
    cleaned = (name or "").strip()
    if not DALTON_NAME_RE.match(cleaned):
        return False, "任务名称必须符合 `dalton-<slug>` 小写字母/数字/连字符规范。"
    if ticker:
        prefix = f"dalton-{ticker.lower()}-"
        if not cleaned.startswith(prefix):
            return False, f"公司工作区自动任务名称必须以 `{prefix}` 开头。"
    return True, None


def build_company_watch_specs(
    *,
    ticker: str,
    workspace_id: str,
    tier: str = "Core",
    timezone_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return the standard automation definitions for a covered company (PK.15)."""
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    t_upper = ticker.upper()
    t_lower = ticker.lower()
    watch_cron = "0 7,19 * * 1-5" if tier.lower() == "core" else "30 7 * * 1-5"

    return [
        {
            "name": f"dalton-{t_lower}-watch",
            "description": f"{t_upper} 日常信号巡检（零 token 预检通过后唤醒分析师）",
            "trigger_type": "cron",
            "cron_expression": watch_cron,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 10,
            "instruction": (
                f"请阅读本轮 `<dalton_wake>` 中的触发信号，使用 `.agents/skills/dalton-agenda/SKILL.md` 与相关分析技能处理 {t_upper} 的最新事项，"
                "更新公司知识层并在收盘前写入 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "company_watch",
                "ticker": t_upper,
                "tier": tier,
            },
        },
        {
            "name": f"dalton-{t_lower}-delta",
            "description": f"{t_upper} 周度 Thesis Delta 复盘",
            "trigger_type": "cron",
            "cron_expression": "0 21 * * 0",
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 10,
            "instruction": (
                f"请按 `.agents/skills/dalton-thesis-delta/SKILL.md` 对 {t_upper} 过去一周的证据、预期变化与催化剂进展做周度复盘，"
                "必要时更新 `thesis` / `debate` / `agenda` 并追加一条 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "weekly_delta",
                "ticker": t_upper,
                "tier": tier,
            },
        },
        {
            "name": f"dalton-{t_lower}-move-up",
            "description": f"{t_upper} 单日涨幅 ≥ 5% 异动归因",
            "trigger_type": "price",
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 10,
            "trigger_config": {
                "symbol": t_upper,
                "market": "stock",
                "conditions": [
                    {
                        "type": "pct_change_above",
                        "value": 5.0,
                        "reference": "previous_close",
                    }
                ],
                "retrigger": {
                    "mode": "recurring",
                    "cooldown_seconds": 86400,
                },
            },
            "instruction": (
                f"{t_upper} 日内涨幅达到或超过 +5%。请按 `.agents/skills/dalton-stock-move/SKILL.md` 排查基本面、新闻、卖方预期与同行联动原因，"
                "并追加 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "price_move",
                "ticker": t_upper,
                "direction": "up",
            },
        },
        {
            "name": f"dalton-{t_lower}-move-down",
            "description": f"{t_upper} 单日跌幅 ≥ 5% 异动归因",
            "trigger_type": "price",
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 10,
            "trigger_config": {
                "symbol": t_upper,
                "market": "stock",
                "conditions": [
                    {
                        "type": "pct_change_below",
                        "value": 5.0,
                        "reference": "previous_close",
                    }
                ],
                "retrigger": {
                    "mode": "recurring",
                    "cooldown_seconds": 86400,
                },
            },
            "instruction": (
                f"{t_upper} 日内跌幅达到或超过 -5%。请按 `.agents/skills/dalton-stock-move/SKILL.md` 排查基本面、新闻、卖方预期与同行联动原因，"
                "并追加 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "price_move",
                "ticker": t_upper,
                "direction": "down",
            },
        },
    ]


def build_earnings_window_specs(
    *,
    ticker: str,
    workspace_id: str,
    period: str,
    report_at: datetime,
    timezone_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build T-3d preview, T+0 flash, and T+1d deep-update one-shot automations for an earnings release."""
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    t_upper = ticker.upper()
    t_lower = ticker.lower()
    p_slug = re.sub(r"[^a-z0-9]+", "", period.lower()) or "next"
    if report_at.tzinfo is None:
        report_at = report_at.replace(tzinfo=timezone.utc)

    tm3_at = report_at - timedelta(days=3)
    t0_at = report_at + timedelta(minutes=30)
    tp1_at = report_at + timedelta(days=1)

    return [
        {
            "name": f"dalton-{t_lower}-earn-{p_slug}-tm3",
            "description": f"{t_upper} {period} 财报前瞻（T-3）",
            "trigger_type": "once",
            "next_run_at": tm3_at,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 5,
            "instruction": (
                f"{t_upper} 将于 {report_at.date().isoformat()} 披露 {period} 财报。"
                "请按 `.agents/skills/dalton-earnings-preview/SKILL.md` 生成财报前瞻草稿并更新 `agenda` 关注清单。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "earnings_tm3",
                "ticker": t_upper,
                "period": period,
            },
        },
        {
            "name": f"dalton-{t_lower}-earn-{p_slug}-t0",
            "description": f"{t_upper} {period} 财报快评（T+0）",
            "trigger_type": "once",
            "next_run_at": t0_at,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 5,
            "instruction": (
                f"{t_upper} {period} 财报已进入披露窗口。请按 `.agents/skills/dalton-earnings-flash/SKILL.md` 完成第一反应快评，"
                "对比一致预期与核心 KPI，并追加 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "earnings_t0",
                "ticker": t_upper,
                "period": period,
            },
        },
        {
            "name": f"dalton-{t_lower}-earn-{p_slug}-tp1",
            "description": f"{t_upper} {period} 财报深度更新（T+1）",
            "trigger_type": "once",
            "next_run_at": tp1_at,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "max_failures": 5,
            "instruction": (
                f"请按 `.agents/skills/dalton-earnings-deep/SKILL.md` 结合 {t_upper} {period} 10-Q/10-K 与电话会纪要，"
                "完成模型更新、深度复盘草稿与知识层更新。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "earnings_tp1",
                "ticker": t_upper,
                "period": period,
            },
        },
    ]


def build_global_director_specs(
    *,
    timezone_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build Home-level automations: earnings calendar scan, daily director, weekly director (PK.15/PK.19)."""
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    return [
        {
            "name": "dalton-earnings-calendar",
            "description": "Dalton 覆盖池财报日历周度刷新并排期 T-3 / T+0 / T+1",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * 1",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "new",
            "max_failures": 10,
            "instruction": (
                "请按 `.agents/skills/dalton-earnings-calendar/SKILL.md` 扫描覆盖池未来 21 天的财报披露日，"
                "更新 `dalton/earnings_calendar.md` 并为即将披露的 Core 公司排期 T-3 / T+0 / T+1 任务。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "calendar",
            },
        },
        {
            "name": "dalton-director-daily",
            "description": "Dalton 研究总监每日晨会调度与覆盖总览生成",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * 1-5",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "pinned",
            "max_failures": 10,
            "instruction": (
                "请按 `.agents/skills/dalton-research-director/SKILL.md` 执行每日晨会：\n"
                "1. 调用 `dalton_coverage_board` 获取全覆盖公司状态与今日剩余额度；\n"
                "2. 对需要深挖或复盘的工作区通过 ` delegate_to_workspace ` 派单；\n"
                "3. 调用 `dalton_record_plan` 记录今日计划并刷新 `dalton/coverage.html`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "director_daily",
            },
        },
        {
            "name": "dalton-director-weekly",
            "description": "Dalton 研究总监每周全域复盘与自主运行周报",
            "trigger_type": "cron",
            "cron_expression": "0 20 * * 0",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "pinned",
            "max_failures": 10,
            "instruction": (
                "请按 `.agents/skills/dalton-weekly-review/SKILL.md` 与 `.agents/skills/dalton-research-director/SKILL.md` "
                "汇总本周全部覆盖公司的结论分布、待决辩题、催化剂兑现情况与预算使用，生成周度复盘报告。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "director_weekly",
            },
        },
    ]
