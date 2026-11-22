# ruff: noqa: BLE001, S110
"""Automation naming, templates, and idempotent scheduling helpers for Dalton (PK.15)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.dalton.auth import (
    resolve_caller_home_workspace_id,
    verify_workspace_access,
)
from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

DALTON_NAME_RE = re.compile(r"^dalton-[a-z0-9][a-z0-9._-]{1,62}$")

DELTA_TIMEZONE = "Asia/Shanghai"
DELTA_CRON_CORE = "0 11 * * 2-6"
DELTA_CRON_WATCH = "0 11 * * 6"

# 行业单元（P3）：议程探针每 2 小时（工作日、研究时区）检查到期项；资料增量
# 每周六北京时间 11:00 扫描——行业没有一致预期/SEC/股价，采集只走内部资料
# 检索路（dalton-sector-delta），频率对齐公司 Watch 档的每周节奏。
SECTOR_AGENDA_CRON = "0 8-18/2 * * 1-5"
SECTOR_DELTA_CRON = "0 11 * * 6"


def resolve_delta_schedule(tier: str) -> tuple[str, str]:
    """Return ``(cron_expression, timezone)`` for a company's ``dalton-<t>-delta`` task."""
    is_core = str(tier or "").strip().lower() == "core"
    return (DELTA_CRON_CORE if is_core else DELTA_CRON_WATCH), DELTA_TIMEZONE

_inproc_user_locks: dict[str, asyncio.Lock] = {}
_held_user_locks: contextvars.ContextVar[frozenset[tuple[int, str]]] = contextvars.ContextVar(
    "_held_user_locks", default=frozenset()
)
_held_user_conn: contextvars.ContextVar[dict[tuple[int, str], Any] | None] = contextvars.ContextVar(
    "_held_user_conn", default=None
)
_prelock_unit_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_prelock_unit_workspace", default=None
)


@asynccontextmanager
async def _user_automations_lock_for_write(
    user_id: str,
    *,
    conn: Any = None,
    prelock_ws: str | None = None,
):
    tok = _prelock_unit_workspace.set(prelock_ws) if prelock_ws else None
    try:
        async with user_automations_lock(user_id, conn=conn) as active_conn:
            yield active_conn
    finally:
        if tok is not None:
            _prelock_unit_workspace.reset(tok)


class AutomationLockError(RuntimeError):
    """Raised when acquiring the user automation advisory lock fails."""


@asynccontextmanager
async def user_automations_lock(user_id: str, *, conn: Any = None):
    """Serialize read-modify-write on a user's automations via ``auto_db.lock_user_automations``."""
    from src.server.database import automation as auto_db

    uid = str(user_id or "").strip()
    if conn is not None or not uid:
        yield conn
        return

    task = asyncio.current_task()
    task_key = (id(task) if task is not None else 0, uid)
    held = _held_user_locks.get()
    if task_key in held:
        conn_map = _held_user_conn.get() or {}
        yield conn_map.get(task_key)
        return

    inproc_lock = _inproc_user_locks.setdefault(uid, asyncio.Lock())
    lock_acquired = False
    prelock_ws = _prelock_unit_workspace.get()
    try:
        async with inproc_lock, get_db_connection() as db_conn, db_conn.transaction():
            if prelock_ws:
                from src.dalton.database.coverage_units import get_active_unit_by_workspace
                from src.dalton.settings import is_coverage_units_enabled

                if is_coverage_units_enabled():
                    await get_active_unit_by_workspace(
                        prelock_ws, conn=db_conn, for_share=True
                    )
            await auto_db.lock_user_automations(uid, conn=db_conn)
            lock_acquired = True
            tok_held = _held_user_locks.set(held | {task_key})
            prev_map = dict(_held_user_conn.get() or {})
            prev_map[task_key] = db_conn
            tok_conn = _held_user_conn.set(prev_map)
            try:
                yield db_conn
            finally:
                _held_user_conn.reset(tok_conn)
                _held_user_locks.reset(tok_held)
    except Exception as exc:
        if not lock_acquired:
            raise AutomationLockError(f"获取用户自动任务并发锁失败：{exc}") from exc
        raise


async def list_all_user_automations(
    user_id: str,
    *,
    name_prefix: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Fetch all automations for ``user_id`` via ``auto_db.list_all_automations``, optionally filtered by ``name_prefix``."""
    from src.server.database import automation as auto_db

    uid = str(user_id or "").strip()
    if not uid:
        return []

    raw_rows = await auto_db.list_all_automations(uid, conn=conn)
    rows = [dict(r) for r in (raw_rows or []) if isinstance(r, dict)]
    if name_prefix:
        pfx = name_prefix.strip().lower()
        rows = [
            r
            for r in rows
            if str(r.get("name") or "").strip().lower().startswith(pfx)
        ]
    return rows


def validate_automation_name(name: str, ticker: str | None = None) -> tuple[bool, str | None]:
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
    timezone_name: str | None = None,
    unit_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the standard automation definitions for a covered company (PK.15 / A13)."""
    t_upper = ticker.upper()
    t_lower = ticker.lower()
    is_cnhk = t_upper.endswith((".HK", ".SS", ".SZ", ".SH"))
    tz = timezone_name or ("Asia/Shanghai" if is_cnhk else "America/New_York")
    watch_cron = "*/30 6-20 * * 1-5" if tier.lower() == "core" else "30 7 * * 1-5"
    delta_cron, delta_tz = resolve_delta_schedule(tier)
    clean_unit_id = str(unit_id or "").strip()

    def _with_unit(meta: dict[str, Any]) -> dict[str, Any]:
        if clean_unit_id:
            meta["unit_id"] = clean_unit_id
        return meta

    return [
        {
            "name": f"dalton-{t_lower}-watch",
            "description": f"{t_upper} 日常信号巡检（零 token 预检通过后唤醒分析师）",
            "trigger_type": "cron",
            "cron_expression": watch_cron,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "instruction": (
                f"请阅读本轮 `<dalton_wake>` 中的触发信号，使用 `.agents/skills/dalton-agenda/SKILL.md` 与相关分析技能处理 {t_upper} 的最新事项，"
                "更新公司知识层并在收盘前写入 `knowledge_append_verdict`。"
            ),
            "metadata": _with_unit(
                {
                    "dalton_managed": True,
                    "dalton_kind": "company_watch",
                    "ticker": t_upper,
                    "tier": tier,
                }
            ),
        },
        {
            "name": f"dalton-{t_lower}-delta",
            "description": f"{t_upper} 盘后 Thesis Delta 复盘",
            "trigger_type": "cron",
            "cron_expression": delta_cron,
            "timezone": delta_tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "instruction": (
                f"请按 `.agents/skills/dalton-thesis-delta/SKILL.md` 对 {t_upper} 最新证据、预期变化与催化剂进展做复盘，"
                "必要时更新 `thesis` / `debate` / `agenda` 并追加一条 `knowledge_append_verdict`。"
            ),
            "metadata": _with_unit(
                {
                    "dalton_managed": True,
                    "dalton_kind": "weekly_delta",
                    "ticker": t_upper,
                    "tier": tier,
                }
            ),
        },
        {
            "name": f"dalton-{t_lower}-move-up",
            "description": f"{t_upper} 单日涨幅 ≥ 4% 异动归因",
            "trigger_type": "price",
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "trigger_config": {
                "symbol": t_upper,
                "market": "stock",
                "conditions": [
                    {
                        "type": "pct_change_above",
                        "value": 4.0,
                        "reference": "previous_close",
                    }
                ],
                "retrigger": {
                    "mode": "recurring",
                    "cooldown_seconds": 86400,
                },
            },
            "instruction": (
                f"{t_upper} 日内涨幅达到或超过 +4%。请按 `.agents/skills/dalton-stock-move/SKILL.md` 第一步先用 `web_search` 与 `x_search` 搜最新新闻和 X（当天与前一天）拿到“今天发生了什么”，再去查基本面、卖方预期与同行联动原因，"
                "并追加 `knowledge_append_verdict`。"
            ),
            "metadata": _with_unit(
                {
                    "dalton_managed": True,
                    "dalton_kind": "price_move",
                    "ticker": t_upper,
                    "direction": "up",
                }
            ),
        },
        {
            "name": f"dalton-{t_lower}-move-down",
            "description": f"{t_upper} 单日跌幅 ≥ 4% 异动归因",
            "trigger_type": "price",
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "trigger_config": {
                "symbol": t_upper,
                "market": "stock",
                "conditions": [
                    {
                        "type": "pct_change_below",
                        "value": 4.0,
                        "reference": "previous_close",
                    }
                ],
                "retrigger": {
                    "mode": "recurring",
                    "cooldown_seconds": 86400,
                },
            },
            "instruction": (
                f"{t_upper} 日内跌幅达到或超过 -4%。请按 `.agents/skills/dalton-stock-move/SKILL.md` 第一步先用 `web_search` 与 `x_search` 搜最新新闻和 X（当天与前一天）拿到“今天发生了什么”，再去查基本面、卖方预期与同行联动原因，"
                "并追加 `knowledge_append_verdict`。"
            ),
            "metadata": _with_unit(
                {
                    "dalton_managed": True,
                    "dalton_kind": "price_move",
                    "ticker": t_upper,
                    "direction": "down",
                }
            ),
        },
    ]


def build_sector_watch_specs(
    *,
    unit_id: str,
    slug: str,
    display_name: str,
    workspace_id: str,
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return the two standard automations for a sector coverage unit (P3).

    Agenda due + material delta only — a sector never runs the securities
    probes (SEC / 港A 公告 / 财报 / 股价): those are subscribed company-side
    once, and reach sectors as P2 cross-unit candidates. Names are
    ``dalton-sector-<slug>-<kind>``: four segments, so neither
    ``_TICKER_FROM_AUTO_NAME_RE`` nor ``preflight._extract_ticker`` can read a
    ticker out of them, and metadata carries ``subject_kind``/``unit_id`` so
    the sector path never falls back to name-guessing. ``timezone_name`` is
    the resolved sector due timezone (doc ``research_tz`` → user tz; see
    ``autonomy.sector``).
    """
    s_slug = re.sub(r"[^a-z0-9._-]+", "-", str(slug or "").strip().lower()).strip("-.") or "sector"
    name = str(display_name or s_slug).strip() or s_slug
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    return [
        {
            "name": f"dalton-sector-{s_slug}-agenda",
            "description": f"{name} 行业议程到期检查（验证项/待查问题/催化剂）",
            "trigger_type": "cron",
            "cron_expression": SECTOR_AGENDA_CRON,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "instruction": (
                f"请阅读本轮 `<dalton_wake>` 中的触发信号，使用 `.agents/skills/dalton-agenda/SKILL.md` 处理行业 {name} 的到期议程"
                "（到期验证项、待查问题、催化剂窗口），更新行业知识层（thesis/agenda/drivers）并追加 `knowledge_append_verdict`。"
                "行业不跑证券探针：成员公司的公告/财报/股价事件由公司侧发现，经跨单元联动进入本工作区。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "sector_watch",
                "subject_kind": "sector",
                "unit_id": str(unit_id),
                "sector_slug": s_slug,
                "sector_name": name,
                "research_tz": tz,
            },
        },
        {
            "name": f"dalton-sector-{s_slug}-delta",
            "description": f"{name} 行业资料增量周扫（内部检索，无证券探针）",
            "trigger_type": "cron",
            "cron_expression": SECTOR_DELTA_CRON,
            "timezone": DELTA_TIMEZONE,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
            "instruction": (
                f"请按 `.agents/skills/dalton-sector-delta/SKILL.md` 对行业 {name} 做内部资料增量扫描"
                "（行业关键词与成员公司名为检索条件，不含 SEC/公告/股价），有实质增量时更新 `thesis` / `debate` / `agenda` 并追加 `knowledge_append_verdict`。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "sector_delta",
                "subject_kind": "sector",
                "unit_id": str(unit_id),
                "sector_slug": s_slug,
                "sector_name": name,
                "research_tz": tz,
            },
        },
    ]


def _offset_trading_days(
    dt: datetime,
    trading_days: int,
    *,
    hour: int,
    minute: int = 0,
) -> datetime:
    """Shift ``dt`` by ``trading_days`` (Mon–Fri only, skipping Sat/Sun) and set local time to ``hour:minute:00`` (P0I §5 #6)."""
    step = 1 if trading_days >= 0 else -1
    remaining = abs(int(trading_days))
    cur = dt
    while remaining > 0:
        cur = cur + timedelta(days=step)
        if cur.weekday() < 5:
            remaining -= 1
    while cur.weekday() >= 5:
        cur = cur + timedelta(days=step)
    return cur.replace(hour=hour, minute=minute, second=0, microsecond=0)


def build_earnings_window_specs(
    *,
    ticker: str,
    workspace_id: str,
    period: str,
    report_at: datetime | str,
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    """Build T-3d preview, T+0 flash, and T+1d deep-update one-shot automations for an earnings release."""
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    t_upper = ticker.upper()
    t_lower = ticker.lower()
    p_slug = re.sub(r"[^a-z0-9]+", "", period.lower()) or "next"
    if isinstance(report_at, str):
        report_at = datetime.fromisoformat(report_at)
    if report_at.tzinfo is None:
        try:
            report_at = report_at.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            report_at = report_at.replace(tzinfo=UTC)
    else:
        try:
            report_at = report_at.astimezone(ZoneInfo(tz))
        except Exception:
            pass

    tm3_at = _offset_trading_days(report_at, -3, hour=8, minute=0)
    t0_at = report_at + timedelta(minutes=30)
    tp1_at = _offset_trading_days(report_at, 1, hour=20, minute=0)

    return [
        {
            "name": f"dalton-{t_lower}-earn-{p_slug}-tm3",
            "description": f"{t_upper} {period} 财报前瞻（T-3）",
            "trigger_type": "once",
            "next_run_at": tm3_at,
            "timezone": tz,
            "workspace_id": workspace_id,
            "thread_strategy": "new",
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
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    """Build Home-level automations: earnings calendar scan, daily director, weekly director (PK.15/PK.19/A13)."""
    tz = timezone_name or get_dalton_settings().autonomy.timezone
    return [
        {
            "name": "dalton-earnings-calendar",
            "description": "Dalton 覆盖池财报日历周度刷新并排期 T-3 / T+0 / T+1",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "new",
            "instruction": (
                "请按 `.agents/skills/dalton-earnings-calendar/SKILL.md` 扫描覆盖池未来 21 天的财报披露日，"
                "维护 `dalton/earnings-calendar.json` 台账，并为即将披露的 Core 公司排期 T-3 / T0 / T+1 任务。"
                "收尾先调用 `dalton_publish_earnings_calendar` 发布台账镜像、"
                "用 `dalton_resolve_signals(probe=\"calendar\", ...)` 关闭本周日历唤醒信号，"
                "再写推送稿完成两轮语言审查，最后用 `dalton_check_done(workflow=\"dalton-earnings-calendar\", ...)` 核对通过后汇报完成。"
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
            "cron_expression": "30 7 * * 1-5",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "pinned",
            "instruction": (
                "请按 `.agents/skills/dalton-research-director/SKILL.md` 执行每日晨会：\n"
                "1. 调用 `dalton_coverage_board` 获取全覆盖公司状态、候选任务评分（`candidates`）与今日剩余额度；\n"
                "2. 对需要深挖或复盘的工作区通过 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)` 派单；\n"
                "3. 调用 `dalton_record_plan` 记录今日计划（写入 `dalton/plans/<date>.md`），并将看板 JSON 写入 `dalton/board.json` 后运行 `python3 .agents/skills/dalton-research-director/scripts/render_coverage.py --in dalton/board.json --out dalton/coverage.html` 刷新 `dalton/coverage.html`。"
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
            "cron_expression": "0 10 * * 6",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "pinned",
            "instruction": (
                "请按 `.agents/skills/dalton-weekly-review/SKILL.md` 与 `.agents/skills/dalton-research-director/SKILL.md` "
                "汇总本周全部覆盖公司的结论分布、待决辩题、催化剂兑现情况与预算使用，生成周度复盘报告。"
            ),
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "director_weekly",
            },
        },
        {
            "name": "dalton-data-health",
            "description": "Dalton 数据地图每日体检（零 token 预检直接写库，不唤醒智能体）",
            "trigger_type": "cron",
            "cron_expression": "0 7 * * *",
            "timezone": tz,
            "workspace_id": None,
            "thread_strategy": "new",
            "instruction": "执行数据源与数据地图每日体检（由 preflight 零 token 直接完成并跳过唤醒）。",
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "data_health",
            },
        },
    ]


async def schedule_dalton_automation_helper(
    name: str | None = None,
    spec: dict[str, Any] | None = None,
    *,
    user_id: str,
    workspace_id: str | None = None,
    caller_workspace_id: str | None = None,
    entry: dict[str, Any] | None = None,
    ticker: str | None = None,
    role: str = "chief_of_staff",
    action: str | None = None,
    actor_user_id: str | None = None,
    conn: Any = None,
    via_rest: bool = False,
) -> dict[str, Any]:
    """Idempotently create, update, pause, resume, or list Dalton (`dalton-*`) automations (G4.5).

    Supports both:
    - ``schedule_dalton_automation_helper(name, spec, user_id=...)``
    - ``schedule_dalton_automation_helper(user_id=..., workspace_id=..., name=..., entry=...)``
    """
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    from src.dalton.company_identity import is_company_system_user
    from src.dalton.database.company_automations import resolve_automation_scope_for_write
    from src.server.database import automation as auto_db
    from src.server.database.home_workspace import is_home_row

    if is_company_system_user(user_id):
        raise ValueError("Company system identity cannot schedule automations via personal scheduler")

    merged_spec: dict[str, Any] = dict(spec or entry or {})
    if str(merged_spec.get("scope") or "").strip().lower() == "company" or is_company_system_user(
        merged_spec.get("user_id")
    ):
        return {
            "ok": False,
            "error": "forbidden_company_scope",
            "message": "客户端不能通过提交 scope 或 user_id 将任务转到公司身份。",
        }
    if "user_id" in merged_spec and str(merged_spec.get("user_id") or "").strip() not in ("", user_id):
        return {
            "ok": False,
            "error": "forbidden_owner_override",
            "message": "禁止通过 spec 指定其他用户的 user_id。",
        }
    for _protected_key in (
        "scope",
        "user_id",
        "created_by",
        "updated_by",
        "audit_history",
        "config_version",
        "active_version",
        "notify_storage_mode",
        "deleted_at",
        "deleted_by",
    ):
        merged_spec.pop(_protected_key, None)
    resolved_action = str(action or merged_spec.get("action") or "upsert").strip().lower()
    resolved_name = str(name or merged_spec.get("name") or "").strip()
    target_auto_id = str(merged_spec.get("automation_id") or "").strip()
    raw_ws = workspace_id if workspace_id is not None else merged_spec.get("workspace_id")
    ws_id_str = str(raw_ws or "").strip()
    home_job_names = {str(s["name"]) for s in build_global_director_specs()}

    if role != "chief_of_staff":
        if caller_workspace_id is not None:
            caller_ws = str(caller_workspace_id or "").strip()
            if not caller_ws or caller_ws.lower() == "home":
                return {
                    "ok": False,
                    "error": "forbidden_workspace",
                    "message": "公司分析师必须在当前公司工作区内调用此工具。",
                }
            if ws_id_str and ws_id_str != caller_ws:
                if ws_id_str.lower() == "home":
                    return {
                        "ok": False,
                        "error": "forbidden_scope",
                        "message": "公司分析师只能操作当前工作区的任务，禁止操作 Home 全局任务。",
                    }
                return {
                    "ok": False,
                    "error": "forbidden_workspace",
                    "message": "公司分析师只能操作当前工作区的任务，禁止跨工作区操作。",
                }
            ws_id_str = caller_ws
        if resolved_action != "list" and (not ws_id_str or ws_id_str.lower() == "home"):
            return {
                "ok": False,
                "error": "forbidden_scope",
                "message": "公司分析师只能操作当前工作区的任务，禁止操作 Home 全局任务。",
            }

    is_home_target = False
    ws_row: dict[str, Any] | None = None
    unit_probe: dict[str, Any] | None = None
    from src.dalton.autonomy.system_jobs import is_pm_user
    from src.dalton.database.coverage_units import get_active_unit_by_workspace
    from src.dalton.settings import (
        is_coverage_units_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import ColleagueAccessRevokedError

    units_on = is_coverage_units_enabled()
    supabase_auth_on = is_dalton_supabase_auth_enabled()
    caller_is_pm = bool(units_on and supabase_auth_on and is_pm_user(user_id))

    if resolved_action == "list":
        if ws_id_str and ws_id_str.lower() != "home":
            try:
                ws_row = await verify_workspace_access(
                    ws_id_str, user_id, need="read", channel="web"
                )
            except ColleagueAccessRevokedError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            ws_id: str | None = ws_id_str
        elif ws_id_str.lower() == "home":
            ws_id = await resolve_caller_home_workspace_id(user_id)
            is_home_target = True
        else:
            ws_id = None
    else:
        if not ws_id_str or ws_id_str.lower() == "home":
            ws_id = await resolve_caller_home_workspace_id(user_id)
            is_home_target = True
        else:
            try:
                ws_row = await verify_workspace_access(
                    ws_id_str,
                    user_id,
                    need="read" if caller_is_pm else "write",
                    channel="web",
                )
            except ColleagueAccessRevokedError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            ws_id = ws_id_str
            is_home_target = bool(
                isinstance(ws_row, dict)
                and (is_home_row(ws_row) or ws_row.get("is_home"))
            )
            if caller_is_pm and not is_home_target:
                ws_owner = str((ws_row or {}).get("user_id") or "").strip()
                if ws_owner and ws_owner != user_id:
                    from src.dalton.access_tiers import classify_owner_tier

                    owner_tier = await classify_owner_tier(ws_owner)
                    if owner_tier == "unknown":
                        raise HTTPException(status_code=404, detail="Workspace not found")
                    if owner_tier == "external":
                        raise HTTPException(
                            status_code=403,
                            detail="External member workspace cannot be modified.",
                        )
                unit_probe = await get_active_unit_by_workspace(ws_id, conn=conn)
                if unit_probe is None:
                    try:
                        ws_row = await verify_workspace_access(
                            ws_id_str, user_id, need="write", channel="web"
                        )
                    except ColleagueAccessRevokedError as exc:
                        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Timezone the caller actually named; ``tz`` adds the deployment default for creation.
    # An update without one must keep the stored timezone, not reset it to the default.
    tz_given = str(merged_spec.get("timezone") or merged_spec.get("timezone_name") or "").strip() or None
    tz = tz_given or str(get_dalton_settings().autonomy.timezone)
    resolved_ticker = ticker or (merged_spec.get("metadata") or {}).get("ticker") or merged_spec.get("ticker")

    if resolved_action == "list":
        list_owner = user_id
        if caller_is_pm and ws_id and not is_home_target:
            ws_owner = str((ws_row or {}).get("user_id") or "").strip()
            if ws_owner and ws_owner != user_id:
                from src.dalton.access_tiers import classify_owner_tier

                owner_tier = await classify_owner_tier(ws_owner)
                if owner_tier == "unknown":
                    raise HTTPException(status_code=404, detail="Workspace not found")
                if owner_tier == "external":
                    raise HTTPException(
                        status_code=403,
                        detail="External member workspace cannot be accessed.",
                    )
            list_unit = await get_active_unit_by_workspace(ws_id, conn=conn)
            if list_unit is not None:
                list_lead = str(
                    list_unit.get("lead_analyst_user_id")
                    or list_unit.get("owner_user_id")
                    or ""
                ).strip()
                if list_lead:
                    list_owner = list_lead
        rows = await list_all_user_automations(
            list_owner,
            name_prefix="dalton-",
            conn=conn,
        )
        dalton_rows = [
            {
                "automation_id": str(r.get("automation_id")),
                "name": r.get("name"),
                "trigger_type": r.get("trigger_type"),
                "cron_expression": r.get("cron_expression"),
                "next_run_at": str(r.get("next_run_at") or ""),
                "status": r.get("status"),
                "llm_model": r.get("llm_model"),
                "reasoning_effort": r.get("reasoning_effort"),
            }
            for r in rows
            if str(r.get("name") or "").startswith("dalton-")
            and (not ws_id or str(r.get("workspace_id") or "") == str(ws_id))
        ]
        return {"ok": True, "count": len(dalton_rows), "automations": dalton_rows}

    if not resolved_name:
        return {"ok": False, "error": "缺少自动任务名称 `name`。"}

    if resolved_name in home_job_names:
        is_home_target = True

    if is_home_target:
        if role != "chief_of_staff":
            return {
                "ok": False,
                "error": "forbidden_scope",
                "message": "公司分析师只能操作当前工作区的任务，禁止操作 Home 全局任务。",
            }
        from src.dalton.autonomy.system_jobs import is_pm_user

        if resolved_name != "dalton-earnings-calendar" and not is_pm_user(user_id):
            return {
                "ok": False,
                "error": "forbidden_pm",
                "message": "只有 PM 用户可以修改或启停 Home 全局任务。",
            }
    elif role != "chief_of_staff":
        from src.dalton.autonomy.tracking import _resolve_workspace_ticker

        ws_ticker = ""
        if ws_id:
            try:
                ws_ticker = str(await _resolve_workspace_ticker(ws_id) or "").strip().upper()
            except Exception:
                return {
                    "ok": False,
                    "error": "forbidden_workspace",
                    "message": f"无法解析当前工作区（{ws_id}）对应的公司 ticker，拒绝操作。",
                }
        if not ws_ticker:
            return {
                "ok": False,
                "error": "forbidden_workspace",
                "message": f"无法解析当前工作区（{ws_id}）对应的公司 ticker，拒绝操作。",
            }
        if resolved_ticker and str(resolved_ticker).strip().upper() != ws_ticker:
            return {
                "ok": False,
                "error": "forbidden_workspace",
                "message": f"公司分析师只能操作当前工作区（{ws_ticker}）的任务，禁止操作 {resolved_ticker}。",
            }
        resolved_ticker = ws_ticker

    skip_upfront_name_check = bool(
        target_auto_id and resolved_action in ("pause", "resume")
    )
    if not skip_upfront_name_check:
        valid, err_msg = validate_automation_name(
            resolved_name,
            ticker=str(resolved_ticker) if (resolved_ticker and role != "chief_of_staff") else None,
        )
        if not valid:
            return {"ok": False, "error": err_msg}

    name_prefix = (
        f"dalton-{str(resolved_ticker).lower()}-"
        if resolved_ticker and resolved_name.startswith(f"dalton-{str(resolved_ticker).lower()}-")
        else resolved_name
    )

    pending_report_finalize: tuple[Any, dict[str, Any], str | None] | None = None
    pending_result: dict[str, Any] | None = None

    from src.dalton.autonomy.classification import (
        is_managed_unit_automation,
        is_managed_unit_automation_for_unit,
    )

    eff_meta_check: dict[str, Any] = dict(merged_spec.get("metadata") or {})
    if resolved_ticker and not eff_meta_check.get("ticker"):
        eff_meta_check["ticker"] = str(resolved_ticker).strip().upper()

    task_owner = user_id
    if units_on and not is_home_target and ws_id:
        if unit_probe is None:
            unit_probe = await get_active_unit_by_workspace(ws_id, conn=conn)
        if caller_is_pm and unit_probe is not None:
            probe_lead = str(
                unit_probe.get("lead_analyst_user_id")
                or unit_probe.get("owner_user_id")
                or ""
            ).strip()
            if probe_lead and probe_lead != user_id:
                lead_pre_rows = await list_all_user_automations(
                    probe_lead,
                    name_prefix=name_prefix,
                    conn=conn,
                )
                existing_lead_pre = next(
                    (
                        r
                        for r in lead_pre_rows
                        if r.get("name") == resolved_name
                        and (
                            (not ws_id or not r.get("workspace_id") or str(r.get("workspace_id")) == str(ws_id))
                            if is_home_target or role == "chief_of_staff"
                            else str(r.get("workspace_id") or "") == str(ws_id)
                        )
                    ),
                    None,
                )
                lead_pre_meta = (
                    dict(existing_lead_pre.get("metadata") or {})
                    if isinstance(existing_lead_pre, dict)
                    and isinstance(existing_lead_pre.get("metadata"), dict)
                    else {}
                )
                combined_lead_meta = {**lead_pre_meta, **eff_meta_check}
                for mk, mv in list(combined_lead_meta.items()):
                    if mv is None:
                        combined_lead_meta.pop(mk, None)
                if (
                    existing_lead_pre is not None
                    and is_managed_unit_automation_for_unit(existing_lead_pre, unit=unit_probe)
                ) or is_managed_unit_automation_for_unit(
                    name=resolved_name,
                    metadata=combined_lead_meta,
                    unit=unit_probe,
                ):
                    task_owner = probe_lead
                else:
                    try:
                        await verify_workspace_access(
                            ws_id, user_id, need="write", channel="web"
                        )
                    except ColleagueAccessRevokedError as exc:
                        raise HTTPException(status_code=403, detail=str(exc)) from exc

    should_check_unit_tx = bool(
        not is_home_target
        and ws_id
        and units_on
        and (
            unit_probe is not None
            or is_managed_unit_automation(name=resolved_name, metadata=eff_meta_check)
        )
    )

    async with _user_automations_lock_for_write(
        task_owner,
        conn=conn,
        prelock_ws=str(ws_id) if should_check_unit_tx else None,
    ) as active_conn:
        conn_kw = {"conn": active_conn} if active_conn is not None else {}
        unit: dict[str, Any] | None = None
        if should_check_unit_tx:
            unit = await get_active_unit_by_workspace(
                ws_id, conn=active_conn, for_share=True
            )
            if caller_is_pm and task_owner != user_id:
                if unit is None:
                    return {
                        "ok": False,
                        "error": "unit_unbound",
                        "message": (
                            f"工作区 {ws_id} 的覆盖单元已解绑或归档，请刷新后重试。"
                        ),
                    }
                locked_lead = str(
                    unit.get("lead_analyst_user_id")
                    or unit.get("owner_user_id")
                    or ""
                ).strip()
                if locked_lead and locked_lead != task_owner:
                    return {
                        "ok": False,
                        "error": "not_unit_lead_analyst",
                        "message": (
                            f"工作区 {ws_id} 已绑定覆盖单元，当前主分析师已变更为 {locked_lead}，请重试。"
                        ),
                    }
        rows = await list_all_user_automations(
            task_owner,
            name_prefix=(
                None if skip_upfront_name_check else name_prefix
            ),
            conn=active_conn,
        )
        def _ws_key(value: Any) -> str:
            raw = str(value or "").strip()
            try:
                return str(uuid.UUID(raw)) if raw else ""
            except ValueError:
                return raw.lower()

        def _matches_requested_workspace(row: Mapping[str, Any]) -> bool:
            row_ws = _ws_key(row.get("workspace_id"))
            req_ws = _ws_key(ws_id)
            if is_home_target or role == "chief_of_staff":
                return (not req_ws) or (not row_ws) or (row_ws == req_ws)
            return row_ws == req_ws

        if skip_upfront_name_check:
            existing = next(
                (
                    r
                    for r in rows
                    if str(r.get("automation_id") or "").strip() == target_auto_id
                    and _matches_requested_workspace(r)
                ),
                None,
            )
            if existing is None:
                return {"ok": False, "error": f"未找到任务 `{resolved_name}`"}
        else:
            existing = next(
                (
                    r
                    for r in rows
                    if r.get("name") == resolved_name and _matches_requested_workspace(r)
                ),
                None,
            )

        effective_unit = unit
        if existing is not None and units_on:
            actual_existing_ws = str(existing.get("workspace_id") or "").strip()
            if not actual_existing_ws:
                effective_unit = None
            elif actual_existing_ws != str(ws_id or "").strip():
                effective_unit = await get_active_unit_by_workspace(
                    actual_existing_ws, conn=active_conn, for_share=True
                )

        if skip_upfront_name_check:
            valid_after, err_after = validate_automation_name(
                str((existing or {}).get("name") or resolved_name),
                ticker=str(resolved_ticker) if (resolved_ticker and role != "chief_of_staff") else None,
            )
            if not valid_after and not (
                existing is not None
                and units_on
                and effective_unit is not None
                and is_managed_unit_automation_for_unit(existing, unit=effective_unit)
            ):
                return {"ok": False, "error": err_after}
        if existing is not None or should_check_unit_tx:
            existing_db_meta = (
                dict(existing.get("metadata") or {})
                if isinstance(existing, dict) and isinstance(existing.get("metadata"), dict)
                else {}
            )
            combined_unit_meta = {**existing_db_meta, **eff_meta_check}
            for mk, mv in list(combined_unit_meta.items()):
                if mv is None:
                    combined_unit_meta.pop(mk, None)
            if effective_unit is not None:
                is_unit_managed = (
                    (existing is not None and is_managed_unit_automation_for_unit(existing, unit=effective_unit))
                    or is_managed_unit_automation_for_unit(
                        name=resolved_name,
                        metadata=combined_unit_meta,
                        unit=effective_unit,
                    )
                )
                if is_unit_managed:
                    lead_uid = str(
                        effective_unit.get("lead_analyst_user_id")
                        or effective_unit.get("owner_user_id")
                        or ""
                    ).strip()
                    if lead_uid and lead_uid != str(task_owner or "").strip():
                        unit_ws_label = str(
                            (existing.get("workspace_id") if existing is not None else None) or ws_id or ""
                        ).strip()
                        return {
                            "ok": False,
                            "error": "not_unit_lead_analyst",
                            "message": (
                                f"工作区 {unit_ws_label} 已绑定覆盖单元，受管自动任务 {resolved_name} "
                                f"仅可由当前主分析师（{lead_uid}）持有或修改。"
                            ),
                        }
        if existing is not None and not is_home_target:
            existing_ws = str(existing.get("workspace_id") or "").strip()
            if not existing_ws or str(existing.get("name") or "") in home_job_names:
                if role != "chief_of_staff":
                    return {
                        "ok": False,
                        "error": "forbidden_scope",
                        "message": "公司分析师只能操作当前工作区的任务，禁止操作 Home 全局任务。",
                    }
                from src.dalton.autonomy.system_jobs import is_pm_user

                if (
                    str(existing.get("name") or "") != "dalton-earnings-calendar"
                    and not is_pm_user(user_id)
                ):
                    return {
                        "ok": False,
                        "error": "forbidden_pm",
                        "message": "只有 PM 用户可以修改或启停 Home 全局任务。",
                    }

        from src.dalton.services.company_automations import (
            automation_history_context,
            get_current_history_context,
        )

        outer_hist_ctx = get_current_history_context()
        effective_actor_id = (
            str(actor_user_id).strip()
            if actor_user_id is not None and str(actor_user_id).strip()
            else (
                str(outer_hist_ctx.actor_user_id).strip()
                if outer_hist_ctx is not None
                and outer_hist_ctx.has_explicit_actor
                and outer_hist_ctx.actor_user_id
                else user_id
            )
        )

        if resolved_action in ("pause", "resume"):
            if not existing:
                return {"ok": False, "error": f"未找到任务 `{resolved_name}`"}
            from src.server.services.automations.lifecycle import (
                AutomationRefusal,
                apply_locked_pause_schedule,
                apply_locked_resume_schedule,
                pause_update_fields,
                resume_update_fields,
            )

            candidate_row = dict(existing)
            if not candidate_row.get("trigger_type"):
                candidate_row["trigger_type"] = (
                    "cron" if candidate_row.get("cron_expression") else "price"
                )
            try:
                if resolved_action == "pause":
                    new_status = "paused"
                    update_fields = pause_update_fields(candidate_row)
                else:
                    new_status = "active"
                    update_fields = resume_update_fields(candidate_row)
                    existing_meta = existing.get("metadata")
                    if isinstance(existing_meta, Mapping) and (
                        "handoff_paused_reason" in existing_meta
                        or "paused_by_user" in existing_meta
                    ):
                        cleaned_meta = dict(existing_meta)
                        cleaned_meta.pop("handoff_paused_reason", None)
                        cleaned_meta.pop("paused_by_user", None)
                        update_fields["metadata"] = cleaned_meta
            except AutomationRefusal as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                }

            async def _reconcile_locked_schedule_action(
                locked_row: Any,
                effective_kwargs: dict[str, Any],
                _locked_conn: Any,
            ) -> None:
                locked_view = dict(locked_row) if isinstance(locked_row, Mapping) else {}
                # The row may have moved to another workspace between the
                # unlocked read that matched it and this lock; a move takes it
                # out of the workspace (and coverage unit) the caller was
                # checked against, so the write must not land.
                if _ws_key(locked_view.get("workspace_id")) != _ws_key(
                    existing.get("workspace_id")
                ):
                    raise AutomationRefusal(
                        f"任务 `{resolved_name}` 已移到其他工作区，请刷新后重试。"
                    )
                if not locked_view.get("trigger_type"):
                    locked_view["trigger_type"] = (
                        "cron" if locked_view.get("cron_expression") else "price"
                    )
                if resolved_action == "pause":
                    apply_locked_pause_schedule(locked_view, effective_kwargs)
                else:
                    apply_locked_resume_schedule(locked_view, effective_kwargs)
                    locked_meta = locked_view.get("metadata")
                    if isinstance(locked_meta, Mapping) and (
                        "handoff_paused_reason" in locked_meta
                        or "paused_by_user" in locked_meta
                    ):
                        cleaned_locked_meta = dict(locked_meta)
                        cleaned_locked_meta.pop("handoff_paused_reason", None)
                        cleaned_locked_meta.pop("paused_by_user", None)
                        effective_kwargs["metadata"] = cleaned_locked_meta

            try:
                with automation_history_context(
                    actor_user_id=effective_actor_id,
                    action=resolved_action,
                    skip_if_unchanged=True,
                    locked_row_reconciler=_reconcile_locked_schedule_action,
                ):
                    updated = await auto_db.update_automation(
                        str(existing["automation_id"]),
                        task_owner,
                        **update_fields,
                        **conn_kw,
                    )
            except AutomationRefusal as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                }
            return {
                "ok": True,
                "action": resolved_action,
                "automation_id": str((updated or existing).get("automation_id")),
                "name": resolved_name,
                "status": new_status,
            }

        if resolved_action != "upsert":
            return {"ok": False, "error": f"不支持的 action: {resolved_action}"}

        instruction = merged_spec.get("instruction") or merged_spec.get("prompt")
        if not instruction and not existing:
            return {"ok": False, "error": "新建任务必须提供 `instruction`。"}

        # The timezone this run is scheduled in: the one named, else the task's stored one
        # (an update), else the deployment default (a new task).
        run_tz = tz_given or str((existing or {}).get("timezone") or "").strip() or tz
        next_run_raw = merged_spec.get("next_run_at") or merged_spec.get("run_at")
        next_dt: datetime | None = None
        if isinstance(next_run_raw, datetime):
            next_dt = next_run_raw
        elif isinstance(next_run_raw, str) and next_run_raw.strip():
            next_dt = datetime.fromisoformat(next_run_raw.strip())
        if next_dt is not None and next_dt.tzinfo is None:
            try:
                next_dt = next_dt.replace(tzinfo=ZoneInfo(run_tz))
            except Exception:
                next_dt = next_dt.replace(tzinfo=UTC)

        explicit_status = str(merged_spec.get("status") or "").strip().lower()
        cron_for_next = merged_spec.get("cron_expression") or merged_spec.get("cron")
        trigger_for_next = str(merged_spec.get("trigger_type") or ("cron" if cron_for_next else "once"))
        if next_dt is None and trigger_for_next == "cron" and cron_for_next and explicit_status != "paused":
            # The scheduler only claims rows with a non-null next_run_at; a cron job created
            # without one never fires. Compute it the same way the upstream API does.
            from src.server.services.automation_scheduler import AutomationScheduler

            next_dt = AutomationScheduler.calculate_first_run(str(cron_for_next), run_tz or "UTC")

        description = merged_spec.get("description")
        cron_expression = merged_spec.get("cron_expression") or merged_spec.get("cron")
        trigger_type = str(merged_spec.get("trigger_type") or ("cron" if cron_expression else "once"))
        trigger_config = merged_spec.get("trigger_config")
        meta = dict(merged_spec.get("metadata") or {})
        meta.setdefault("dalton_managed", True)
        if resolved_ticker and not meta.get("ticker"):
            meta["ticker"] = str(resolved_ticker).upper()

        from src.server.services.automations.lifecycle import (
            AutomationRefusal,
            _assert_replaced_by_tracker_preserved,
            _reconcile_reasoning_effort,
            _replaced_by_tracker_refusal,
            _safe_model_pref,
        )

        effort_in_spec = "reasoning_effort" in merged_spec
        raw_effort_spec = merged_spec.get("reasoning_effort")
        if isinstance(raw_effort_spec, str):
            norm_raw = raw_effort_spec.strip().lower()
            raw_effort_spec = None if norm_raw in ("", "default", "clear", "null") else norm_raw
        model_in_spec = "llm_model" in merged_spec
        spec_llm_model = merged_spec.get("llm_model")
        if isinstance(spec_llm_model, str):
            spec_llm_model = spec_llm_model.strip() or None
        # Load user model_pref before DB writes if model or effort reconciliation needs it.
        pref_for_effort: dict[str, Any] | None = None
        if effort_in_spec or model_in_spec or existing is None:
            pref_for_effort = await _safe_model_pref(task_owner, None)

        if existing:
            if (replaced_refusal := _replaced_by_tracker_refusal(existing)) is not None:
                return {
                    "ok": False,
                    "error": str(replaced_refusal),
                    "message": str(replaced_refusal),
                }
            existing_meta = (
                dict(existing.get("metadata") or {})
                if isinstance(existing.get("metadata"), dict)
                else {}
            )
            merged_meta = {**existing_meta, **meta}
            try:
                _assert_replaced_by_tracker_preserved(existing_meta, merged_meta)
            except HTTPException as exc:
                if via_rest:
                    raise
                return {
                    "ok": False,
                    "error": "forbidden_replaced_by_tracker",
                    "message": str(exc.detail),
                }
            for mk, mv in list(merged_meta.items()):
                if mv is None and mk != "replaced_by_tracker":
                    merged_meta.pop(mk, None)

            old_rp = existing_meta.get("report_profile")
            new_rp = merged_meta.get("report_profile")
            rp_changed = (
                "report_profile" in meta
                or ("report_profile" in existing_meta and new_rp != old_rp)
            ) and new_rp != old_rp
            report_profile_change = None
            if rp_changed:
                from src.dalton.services.report_delivery import (
                    ReportDeliveryError,
                    validate_report_profile_change,
                )

                target_view = dict(existing)
                if instruction is not None:
                    target_view["instruction"] = instruction
                try:
                    report_profile_change = await validate_report_profile_change(
                        effective_actor_id,
                        target_view,
                        new_rp,
                        via_rest=via_rest,
                        user_confirmed=False,
                        action="update",
                        after_metadata=merged_meta,
                        conn=active_conn,
                    )
                except ReportDeliveryError as exc:
                    if via_rest:
                        raise
                    return {"ok": False, "error": exc.code, "message": exc.message}

            existing_is_paused = str(existing.get("status") or "").strip().lower() == "paused"
            patch: dict[str, Any] = {}
            if instruction is not None:
                patch["instruction"] = instruction
            if description is not None:
                patch["description"] = description
            if cron_expression is not None:
                patch["cron_expression"] = cron_expression
            if tz_given:
                patch["timezone"] = tz_given
            if trigger_config is not None:
                patch["trigger_config"] = trigger_config
            if explicit_status == "paused":
                patch["next_run_at"] = None
            elif next_dt is not None and (not existing_is_paused or explicit_status == "active"):
                patch["next_run_at"] = next_dt
            if rp_changed and report_profile_change is not None:
                merged_meta = dict(report_profile_change.after_metadata)
            if merged_meta or rp_changed:
                patch["metadata"] = merged_meta
            if explicit_status:
                patch["status"] = explicit_status
            if model_in_spec:
                patch["llm_model"] = spec_llm_model
            target_model_for_effort = (
                patch["llm_model"] if model_in_spec else existing.get("llm_model")
            )
            if effort_in_spec or model_in_spec or existing.get("reasoning_effort") is not None:
                try:
                    write_effort, reconciled_effort = await _reconcile_reasoning_effort(
                        task_owner,
                        target_model=target_model_for_effort,
                        effort_set=effort_in_spec,
                        raw_effort=raw_effort_spec,
                        stored_effort=existing.get("reasoning_effort"),
                        is_create=False,
                        model_pref=pref_for_effort,
                        model_changed=(
                            model_in_spec
                            and target_model_for_effort != existing.get("llm_model")
                        ),
                    )
                except AutomationRefusal as exc:
                    if via_rest:
                        raise
                    return {
                        "ok": False,
                        "error": "invalid_reasoning_effort",
                        "message": str(exc),
                    }
                if write_effort:
                    patch["reasoning_effort"] = reconciled_effort
            saved_workspace_id = (
                patch["workspace_id"]
                if "workspace_id" in patch
                else existing.get("workspace_id")
            )
            saved_name = (
                str(patch["name"])
                if "name" in patch and patch["name"] is not None
                else str(existing.get("name") or resolved_name)
            )
            saved_metadata = (
                patch["metadata"]
                if "metadata" in patch
                else existing.get("metadata")
            )
            patch["scope"] = await resolve_automation_scope_for_write(
                task_owner,
                name=saved_name,
                workspace_id=str(saved_workspace_id) if saved_workspace_id else None,
                metadata=saved_metadata if isinstance(saved_metadata, dict) else None,
                conn=active_conn,
            )
            effective_update_action = (
                str(outer_hist_ctx.action)
                if outer_hist_ctx is not None and outer_hist_ctx.has_explicit_action
                else "update"
            )
            report_audit_payload = (
                report_profile_change.build_audit_entry()
                if report_profile_change is not None
                else None
            )
            async def _reconcile_locked_effort(
                locked_row: Mapping[str, Any],
                effective_kwargs: dict[str, Any],
                _locked_conn: Any,
            ) -> None:
                locked_model = locked_row.get("llm_model")
                locked_effort = locked_row.get("reasoning_effort")
                final_model = (
                    effective_kwargs["llm_model"]
                    if "llm_model" in effective_kwargs
                    else locked_model
                )
                if not (effort_in_spec or model_in_spec or locked_effort is not None):
                    return
                write_eff, reconciled_eff = await _reconcile_reasoning_effort(
                    task_owner,
                    target_model=final_model,
                    effort_set=effort_in_spec,
                    raw_effort=raw_effort_spec,
                    stored_effort=locked_effort,
                    is_create=False,
                    model_pref=pref_for_effort,
                    model_changed=(model_in_spec and final_model != locked_model),
                )
                if write_eff:
                    effective_kwargs["reasoning_effort"] = reconciled_eff
                else:
                    effective_kwargs.pop("reasoning_effort", None)

            try:
                with automation_history_context(
                    actor_user_id=effective_actor_id,
                    action=effective_update_action,
                    skip_if_unchanged=True,
                    locked_row_reconciler=_reconcile_locked_effort,
                    report_delivery_audit=report_audit_payload,
                ):
                    updated = await auto_db.update_automation(
                        str(existing["automation_id"]),
                        task_owner,
                        **conn_kw,
                        **patch,
                    )
            except AutomationRefusal as exc:
                if via_rest:
                    raise
                return {
                    "ok": False,
                    "error": "invalid_reasoning_effort",
                    "message": str(exc),
                }
            updated_row = updated or existing
            sync_failed_warning: str | None = None
            if report_profile_change is not None:
                sync_failed_warning = await _sync_report_profile_in_schedule_tx(
                    active_conn,
                    change=report_profile_change,
                    saved_row=updated_row,
                    default_owner_user_id=task_owner,
                )
            result: dict[str, Any] = {
                "ok": True,
                "action": "updated",
                "automation_id": str(updated_row.get("automation_id")),
                "name": resolved_name,
            }
            from src.dalton.services.report_delivery import (
                build_missing_report_profile_warning,
            )

            warning = build_missing_report_profile_warning(
                updated_row.get("instruction"),
                updated_row.get("metadata"),
            )
            if warning:
                result["warning"] = warning
            if report_profile_change is not None:
                pending_report_finalize = (report_profile_change, updated_row, sync_failed_warning)
                pending_result = result
            else:
                return result

        if pending_result is None:
            try:
                _assert_replaced_by_tracker_preserved({}, meta)
            except HTTPException as exc:
                if via_rest:
                    raise
                return {
                    "ok": False,
                    "error": "forbidden_replaced_by_tracker",
                    "message": str(exc.detail),
                }
            if (
                units_on
                and unit is not None
                and str(unit.get("kind") or "").strip().lower() == "company"
                and not meta.get("unit_id")
                and is_managed_unit_automation_for_unit(
                    name=resolved_name, metadata=meta, unit=unit
                )
            ):
                meta["unit_id"] = str(unit["unit_id"])
            clean_meta = {k: v for k, v in meta.items() if v is not None}
            create_report_change = None
            if "report_profile" in clean_meta:
                from src.dalton.services.report_delivery import (
                    ReportDeliveryError,
                    validate_report_profile_change,
                )

                try:
                    create_report_change = await validate_report_profile_change(
                        effective_actor_id,
                        {
                            "user_id": task_owner,
                            "name": resolved_name,
                            "instruction": instruction or "",
                            "metadata": {},
                        },
                        clean_meta.get("report_profile"),
                        via_rest=via_rest,
                        user_confirmed=False,
                        action="create",
                        after_metadata=clean_meta,
                        conn=active_conn,
                    )
                except ReportDeliveryError as exc:
                    if via_rest:
                        raise
                    return {"ok": False, "error": exc.code, "message": exc.message}
            create_meta = (
                dict(create_report_change.after_metadata)
                if create_report_change is not None
                else clean_meta
            )
            resolved_scope = await resolve_automation_scope_for_write(
                task_owner,
                name=resolved_name,
                workspace_id=ws_id,
                metadata=create_meta,
                conn=active_conn,
            )
            create_llm_model = spec_llm_model if model_in_spec else None
            try:
                _, create_reasoning_effort = await _reconcile_reasoning_effort(
                    task_owner,
                    target_model=create_llm_model,
                    effort_set=effort_in_spec,
                    raw_effort=raw_effort_spec,
                    stored_effort=None,
                    is_create=True,
                    model_pref=pref_for_effort,
                )
            except AutomationRefusal as exc:
                if via_rest:
                    raise
                return {
                    "ok": False,
                    "error": "invalid_reasoning_effort",
                    "message": str(exc),
                }
            create_kw: dict[str, Any] = dict(conn_kw)
            if explicit_status and explicit_status != "active":
                create_kw["status"] = explicit_status
            if create_reasoning_effort is not None:
                create_kw["reasoning_effort"] = create_reasoning_effort
            create_audit_payload = (
                create_report_change.build_audit_entry()
                if create_report_change is not None
                else None
            )
            with automation_history_context(
                actor_user_id=effective_actor_id,
                action="create",
                report_delivery_audit=create_audit_payload,
            ):
                created = await auto_db.create_automation(
                    user_id=task_owner,
                    name=resolved_name,
                    description=description,
                    trigger_type=trigger_type,
                    cron_expression=cron_expression,
                    timezone=tz,
                    trigger_config=trigger_config,
                    next_run_at=None if explicit_status == "paused" else next_dt,
                    instruction=instruction or "",
                    workspace_id=ws_id,
                    llm_model=create_llm_model,
                    additional_context=merged_spec.get("additional_context"),
                    thread_strategy=str(merged_spec.get("thread_strategy") or "new"),
                    conversation_thread_id=merged_spec.get("conversation_thread_id"),
                    delivery_config=merged_spec.get("delivery_config"),
                    metadata=create_meta,
                    scope=resolved_scope,
                    created_by=effective_actor_id,
                    **create_kw,
                )
            create_sync_failed_warning: str | None = None
            if create_report_change is not None and isinstance(created, dict):
                create_sync_failed_warning = await _sync_report_profile_in_schedule_tx(
                    active_conn,
                    change=create_report_change,
                    saved_row=created,
                    default_owner_user_id=task_owner,
                )
            created_result: dict[str, Any] = {
                "ok": True,
                "action": "created",
                "automation_id": str(created.get("automation_id")),
                "name": resolved_name,
            }
            from src.dalton.services.report_delivery import (
                build_missing_report_profile_warning,
            )

            warning = build_missing_report_profile_warning(
                created.get("instruction") if isinstance(created, dict) else (instruction or ""),
                created.get("metadata") if isinstance(created, dict) else create_meta,
            )
            if warning:
                created_result["warning"] = warning
            if create_report_change is not None:
                pending_report_finalize = (create_report_change, created, create_sync_failed_warning)
                pending_result = created_result
            else:
                return created_result

    if pending_report_finalize is not None:
        change_obj, saved_row, sync_failed_warning = pending_report_finalize
        from src.dalton.company_identity import is_company_system_user
        from src.dalton.services.report_delivery import evaluate_route_warning_best_effort
        from src.server.database.user import (
            invalidate_user_prefs_cache,
            invalidate_user_profile_cache,
        )

        if not is_company_system_user(change_obj.owner_user_id):
            try:
                await invalidate_user_prefs_cache(change_obj.owner_user_id)
                await invalidate_user_profile_cache(change_obj.owner_user_id)
            except Exception:
                pass
        await change_obj.finalize(automation=saved_row)
        if sync_failed_warning:
            route_warning = sync_failed_warning
        else:
            route_warning = await evaluate_route_warning_best_effort(
                change_obj.owner_user_id,
                saved_row,
                change_obj,
            )
        assert pending_result is not None
        pending_result["route_warning"] = route_warning
    assert pending_result is not None
    return pending_result


async def _sync_report_profile_in_schedule_tx(
    active_conn: Any,
    *,
    change: Any,
    saved_row: dict[str, Any],
    default_owner_user_id: str,
) -> str | None:
    """Sync ``automation:<id>`` routes, refresh snapshot, and claim 10m admin notice slot inside schedule tx."""
    from src.dalton.services.report_delivery import sync_report_profile_after_write_in_tx

    return await sync_report_profile_after_write_in_tx(
        active_conn,
        change=change,
        saved_row=saved_row,
        default_owner_user_id=default_owner_user_id,
    )
