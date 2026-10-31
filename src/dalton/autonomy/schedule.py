# ruff: noqa: BLE001, S110
"""Automation naming, templates, and idempotent scheduling helpers for Dalton (PK.15)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
    try:
        async with inproc_lock, get_db_connection() as db_conn, db_conn.transaction():
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
) -> list[dict[str, Any]]:
    """Return the standard automation definitions for a covered company (PK.15 / A13)."""
    t_upper = ticker.upper()
    t_lower = ticker.lower()
    is_cnhk = t_upper.endswith((".HK", ".SS", ".SZ", ".SH"))
    tz = timezone_name or ("Asia/Shanghai" if is_cnhk else "America/New_York")
    watch_cron = "*/30 6-20 * * 1-5" if tier.lower() == "core" else "30 7 * * 1-5"
    delta_cron, delta_tz = resolve_delta_schedule(tier)

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
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "company_watch",
                "ticker": t_upper,
                "tier": tier,
            },
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
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "weekly_delta",
                "ticker": t_upper,
                "tier": tier,
            },
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
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "price_move",
                "ticker": t_upper,
                "direction": "up",
            },
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
            "metadata": {
                "dalton_managed": True,
                "dalton_kind": "price_move",
                "ticker": t_upper,
                "direction": "down",
            },
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
    conn: Any = None,
) -> dict[str, Any]:
    """Idempotently create, update, pause, resume, or list Dalton (`dalton-*`) automations (G4.5).

    Supports both:
    - ``schedule_dalton_automation_helper(name, spec, user_id=...)``
    - ``schedule_dalton_automation_helper(user_id=..., workspace_id=..., name=..., entry=...)``
    """
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    from src.server.database import automation as auto_db
    from src.server.database.home_workspace import is_home_row

    merged_spec: dict[str, Any] = dict(spec or entry or {})
    resolved_action = str(action or merged_spec.get("action") or "upsert").strip().lower()
    resolved_name = str(name or merged_spec.get("name") or "").strip()
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
    if resolved_action == "list":
        if ws_id_str and ws_id_str.lower() != "home":
            ws_row = await verify_workspace_access(ws_id_str, user_id)
            ws_id: str | None = ws_id_str
        elif ws_id_str.lower() == "home":
            ws_id = await resolve_caller_home_workspace_id(user_id)
        else:
            ws_id = None
    else:
        if not ws_id_str or ws_id_str.lower() == "home":
            ws_id = await resolve_caller_home_workspace_id(user_id)
            is_home_target = True
        else:
            ws_row = await verify_workspace_access(ws_id_str, user_id)
            ws_id = ws_id_str
            is_home_target = bool(
                isinstance(ws_row, dict)
                and (is_home_row(ws_row) or ws_row.get("is_home"))
            )

    tz = str(merged_spec.get("timezone") or merged_spec.get("timezone_name") or get_dalton_settings().autonomy.timezone)
    resolved_ticker = ticker or (merged_spec.get("metadata") or {}).get("ticker") or merged_spec.get("ticker")

    if resolved_action == "list":
        rows = await list_all_user_automations(
            user_id,
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

    async with user_automations_lock(user_id, conn=conn) as active_conn:
        conn_kw = {"conn": active_conn} if active_conn is not None else {}
        rows = await list_all_user_automations(
            user_id,
            name_prefix=name_prefix,
            conn=active_conn,
        )
        existing = next(
            (
                r
                for r in rows
                if r.get("name") == resolved_name
                and (
                    (not ws_id or not r.get("workspace_id") or str(r.get("workspace_id")) == str(ws_id))
                    if is_home_target or role == "chief_of_staff"
                    else str(r.get("workspace_id") or "") == str(ws_id)
                )
            ),
            None,
        )
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

        if resolved_action in ("pause", "resume"):
            if not existing:
                return {"ok": False, "error": f"未找到任务 `{resolved_name}`"}
            new_status = "paused" if resolved_action == "pause" else "active"
            updated = await auto_db.update_automation(
                str(existing["automation_id"]),
                user_id,
                status=new_status,
                **conn_kw,
            )
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

        next_run_raw = merged_spec.get("next_run_at") or merged_spec.get("run_at")
        next_dt: datetime | None = None
        if isinstance(next_run_raw, datetime):
            next_dt = next_run_raw
        elif isinstance(next_run_raw, str) and next_run_raw.strip():
            next_dt = datetime.fromisoformat(next_run_raw.strip())
        if next_dt is not None and next_dt.tzinfo is None:
            try:
                next_dt = next_dt.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                next_dt = next_dt.replace(tzinfo=UTC)

        explicit_status = str(merged_spec.get("status") or "").strip().lower()
        cron_for_next = merged_spec.get("cron_expression") or merged_spec.get("cron")
        trigger_for_next = str(merged_spec.get("trigger_type") or ("cron" if cron_for_next else "once"))
        if next_dt is None and trigger_for_next == "cron" and cron_for_next and explicit_status != "paused":
            # The scheduler only claims rows with a non-null next_run_at; a cron job created
            # without one never fires. Compute it the same way the upstream API does.
            from src.server.services.automation_scheduler import AutomationScheduler

            next_dt = AutomationScheduler.calculate_first_run(str(cron_for_next), tz or "UTC")

        description = merged_spec.get("description")
        cron_expression = merged_spec.get("cron_expression") or merged_spec.get("cron")
        trigger_type = str(merged_spec.get("trigger_type") or ("cron" if cron_expression else "once"))
        trigger_config = merged_spec.get("trigger_config")
        meta = dict(merged_spec.get("metadata") or {})
        meta.setdefault("dalton_managed", True)
        if resolved_ticker and not meta.get("ticker"):
            meta["ticker"] = str(resolved_ticker).upper()

        if existing:
            existing_meta = (
                dict(existing.get("metadata") or {})
                if isinstance(existing.get("metadata"), dict)
                else {}
            )
            merged_meta = {**existing_meta, **meta}
            for mk, mv in list(merged_meta.items()):
                if mv is None:
                    merged_meta.pop(mk, None)

            existing_is_paused = str(existing.get("status") or "").strip().lower() == "paused"
            patch: dict[str, Any] = {}
            if instruction is not None:
                patch["instruction"] = instruction
            if description is not None:
                patch["description"] = description
            if cron_expression is not None:
                patch["cron_expression"] = cron_expression
            if tz:
                patch["timezone"] = tz
            if trigger_config is not None:
                patch["trigger_config"] = trigger_config
            if explicit_status == "paused":
                patch["next_run_at"] = None
            elif next_dt is not None and (not existing_is_paused or explicit_status == "active"):
                patch["next_run_at"] = next_dt
            if merged_meta:
                patch["metadata"] = merged_meta
            if explicit_status:
                patch["status"] = explicit_status
            updated = await auto_db.update_automation(
                str(existing["automation_id"]),
                user_id,
                **conn_kw,
                **patch,
            )
            return {
                "ok": True,
                "action": "updated",
                "automation_id": str((updated or existing).get("automation_id")),
                "name": resolved_name,
            }

        clean_meta = {k: v for k, v in meta.items() if v is not None}
        create_kw: dict[str, Any] = dict(conn_kw)
        if explicit_status and explicit_status != "active":
            create_kw["status"] = explicit_status
        created = await auto_db.create_automation(
            user_id=user_id,
            name=resolved_name,
            description=description,
            trigger_type=trigger_type,
            cron_expression=cron_expression,
            timezone=tz,
            trigger_config=trigger_config,
            next_run_at=None if explicit_status == "paused" else next_dt,
            instruction=instruction or "",
            workspace_id=ws_id,
            llm_model=merged_spec.get("llm_model"),
            additional_context=merged_spec.get("additional_context"),
            thread_strategy=str(merged_spec.get("thread_strategy") or "new"),
            conversation_thread_id=merged_spec.get("conversation_thread_id"),
            delivery_config=merged_spec.get("delivery_config"),
            metadata=clean_meta,
            **create_kw,
        )
        return {
            "ok": True,
            "action": "created",
            "automation_id": str(created.get("automation_id")),
            "name": resolved_name,
        }
