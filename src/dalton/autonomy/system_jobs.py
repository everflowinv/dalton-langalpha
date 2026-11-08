# ruff: noqa: BLE001
"""Unified Dalton system jobs service (company, home, host, ops)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter
import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton.access_tiers import get_user_tier
from src.dalton.autonomy.schedule import (
    build_global_director_specs,
    list_all_user_automations,
    schedule_dalton_automation_helper,
    user_automations_lock,
)
from src.dalton.autonomy.tracking import (
    disable_tracking,
    enable_tracking,
    get_tracking_status,
    resolve_workspace_id_by_ticker,
)
from src.dalton.coverage.identity import _TICKER_FROM_AUTO_NAME_RE
from src.dalton.data_jobs.manifest import (
    DataTaskManifestEntry,
    load_data_task_manifest,
)
from src.dalton.llm.model_prefs import (
    DALTON_NOTIFY_ROUTES_KEY,
    run_with_feishu_group_prefetch,
    validate_dalton_model_preferences,
    validate_dalton_notify_routes_for_user,
)
from src.llms.preferences import TuningError
from src.dalton.settings import (
    get_configured_pm_user_ids,
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_dalton_config,
    is_coverage_units_enabled,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.home_workspace import is_home_id, is_home_row
from src.server.database.pool import get_db_connection
from src.server.database.user import (
    get_user_preferences,
    invalidate_user_prefs_cache,
    lock_user_preferences,
    upsert_user_preferences,
)
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.automations import lifecycle as auto_lifecycle

logger = logging.getLogger(__name__)

HOME_JOB_TITLES: dict[str, str] = {
    "dalton-earnings-calendar": "财报日历扫描",
    "dalton-director-daily": "研究总监每日晨会",
    "dalton-director-weekly": "研究总监每周复盘",
    "dalton-data-health": "数据地图每日体检",
}

DEFAULT_OPS_JOBS: list[dict[str, Any]] = [
    {
        "key": "ops-db-backup",
        "kind": "ops_db_backup",
        "title": "数据库备份",
        "description": "宿主机 launchd 定时备份 PostgreSQL 数据库",
    },
    {
        "key": "ops-backend-watchdog",
        "kind": "ops_backend_watchdog",
        "title": "后端看门狗",
        "description": "宿主机 launchd 监控后端与桥接服务健康状态",
    },
]

_TRACKING_STATUS_TO_JOB_STATUS: dict[str, str] = {
    "enabled": "on",
    "disabled": "off",
    "incomplete": "partial",
}


def is_pm_user(user_id: str) -> bool:
    """Return True if ``user_id`` is a configured PM user."""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return uid in get_dalton_config().pm_user_ids


def require_pm_user(user_id: str) -> None:
    """Raise ``PermissionError`` if Dalton is disabled or ``user_id`` is not in ``pm_user_ids``."""
    cfg = get_dalton_config()
    if not cfg.enabled:
        raise PermissionError("Dalton mode disabled")
    uid = str(user_id or "").strip()
    if not uid or uid not in cfg.pm_user_ids:
        raise PermissionError("Only PM users may perform this action")


def _iso_or_none(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    s = str(val).strip()
    return s or None


def _summarize_last_execution(
    last_exec: dict[str, Any] | None,
    *,
    kind: str = "",
) -> tuple[str | None, str | None]:
    """Return ``(last_run_at_iso, last_result_text)`` from an automation's ``last_execution``."""
    if not isinstance(last_exec, dict) or not last_exec:
        return None, None
    ts = (
        last_exec.get("completed_at")
        or last_exec.get("started_at")
        or last_exec.get("created_at")
        or last_exec.get("scheduled_at")
    )
    ts_str = _iso_or_none(ts)
    st = str(last_exec.get("status") or "").strip().lower()
    skip_reason = str(last_exec.get("skip_reason") or "").strip().lower()
    excerpt = str(last_exec.get("excerpt") or "").strip()
    err_msg = str(
        last_exec.get("error_message")
        or last_exec.get("failure_reason")
        or last_exec.get("error")
        or ""
    ).strip()

    if st == "completed":
        return ts_str, (excerpt[:80] if excerpt else "执行完成")
    if st == "skipped":
        if kind == "data_health" and skip_reason in ("", "precheck", "skip"):
            reason_text = str(last_exec.get("reason") or excerpt or err_msg or "").strip()
            lower_reason = reason_text.lower()
            if "timeout" in lower_reason or "超时" in reason_text:
                return ts_str, "体检超时"
            if (
                "失败" in reason_text
                or "异常" in reason_text
                or "error" in lower_reason
                or "failed" in lower_reason
            ):
                if reason_text.startswith("体检失败："):
                    return ts_str, reason_text[:80]
                detail = reason_text.split("：", 1)[1].strip() if "：" in reason_text else reason_text
                return ts_str, f"体检失败：{detail}"[:80]
            if reason_text.startswith(("体检完成", "数据地图体检完成")):
                return ts_str, reason_text.removeprefix("数据地图")[:80]
            if lower_reason.startswith("data_health completed"):
                suffix = reason_text[len("data_health completed") :].strip()
                return ts_str, (f"体检完成 {suffix}"[:80] if suffix else "体检完成")
            if reason_text:
                return ts_str, reason_text[:80]
            return ts_str, f"已跳过（{skip_reason or 'skip'}）"
        if skip_reason == "precheck":
            return ts_str, "预检完成（无新信号跳过）"
        return ts_str, f"已跳过（{skip_reason or 'skip'}）"
    if st == "failed":
        return ts_str, f"失败：{err_msg[:80]}" if err_msg else "执行失败"
    if st in ("running", "pending", "waiting"):
        return ts_str, "运行中"
    return ts_str, st or None


def _conversations_readonly_config() -> dict[str, Any]:
    """Read-only conversation archive extraction parameters from ``src/dalton/dalton.yaml``."""
    cfg = get_dalton_config().conversations
    return {
        "extractor_model": cfg.extractor_model,
        "model": cfg.extractor_model,
        "prompt_version": cfg.prompt_version,
        "extract_concurrency": cfg.extract_concurrency,
        "concurrency": cfg.extract_concurrency,
    }


async def _call_host_bridge(
    method: str,
    path: str,
    *,
    timeout_s: float = 8.0,
) -> tuple[int, dict[str, Any] | None]:
    """Call the host-side researchdata bridge with token authentication."""
    base_url = get_dalton_bridge_base_url().rstrip("/")
    token = get_dalton_bridge_admin_token()
    url = f"{base_url}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dalton-Bridge-Token"] = token
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.request(method.upper(), url, headers=headers)
        try:
            payload = resp.json()
        except Exception:
            payload = None
        return resp.status_code, (payload if isinstance(payload, dict) else None)


async def _fetch_host_conversation_archive_job() -> dict[str, Any]:
    """Build the ``host`` scope system job entry for ``conversation-archive``."""
    conv_cfg = _conversations_readonly_config()
    base_item: dict[str, Any] = {
        "key": "conversation-archive",
        "kind": "conversation_archive",
        "scope": "host",
        "title": "团队对话归档",
        "description": "每日在宿主机增量抽取 OpenClaw 团队对话并写入投研记忆库",
        "schedule": None,
        "status": "unknown",
        "last_run_at": None,
        "last_result": None,
        "next_run_at": None,
        "can_toggle": True,
        "can_run_now": True,
        "config": conv_cfg,
        "exit_code": None,
        "log_tail": "",
        "running": False,
    }
    try:
        status_code, data = await _call_host_bridge("GET", "/jobs/conversation-archive")
        if status_code == 200 and isinstance(data, dict):
            raw_status = str(data.get("status") or "").strip().lower()
            if raw_status not in ("on", "off", "partial", "unknown"):
                raw_status = "on" if data.get("enabled", True) else "off"
            exit_code = data.get("exit_code")
            last_result = data.get("last_result")
            if not last_result and exit_code is not None:
                last_result = "成功 (exit 0)" if exit_code == 0 else f"失败 (exit {exit_code})"
            if data.get("running"):
                last_result = "运行中"
            base_item.update(
                {
                    "status": raw_status,
                    "enabled": bool(data.get("enabled", raw_status == "on")),
                    "last_run_at": data.get("last_run_at"),
                    "last_result": last_result,
                    "next_run_at": data.get("next_run_at"),
                    "exit_code": exit_code,
                    "log_tail": str(data.get("log_tail") or ""),
                    "running": bool(data.get("running", False)),
                }
            )
    except Exception as exc:
        logger.debug("[DALTON_SYSTEM_JOBS] Host bridge conversation-archive unreachable: %s", exc)
    return base_item


async def _fetch_ops_jobs() -> list[dict[str, Any]]:
    """Build the ``ops`` scope read-only job entries, falling back to ``unknown`` on error."""
    by_key: dict[str, dict[str, Any]] = {}
    for spec in DEFAULT_OPS_JOBS:
        by_key[spec["key"]] = {
            "key": spec["key"],
            "kind": spec["kind"],
            "scope": "ops",
            "title": spec["title"],
            "description": spec["description"],
            "status": "unknown",
            "last_run_at": None,
            "last_result": None,
            "next_run_at": None,
            "can_toggle": False,
            "can_run_now": False,
        }
    try:
        status_code, data = await _call_host_bridge("GET", "/jobs/ops", timeout_s=5.0)
        if status_code == 200 and isinstance(data, dict):
            raw_jobs = data.get("jobs")
            if isinstance(raw_jobs, list):
                for item in raw_jobs:
                    if not isinstance(item, dict) or not item.get("key"):
                        continue
                    k = str(item["key"]).strip()
                    st = str(item.get("status") or "unknown").strip().lower()
                    if st not in ("on", "off", "partial", "unknown"):
                        st = "unknown"
                    target = by_key.get(k) or {
                        "key": k,
                        "kind": str(item.get("kind") or "ops"),
                        "scope": "ops",
                        "title": str(item.get("title") or k),
                        "description": str(item.get("description") or ""),
                        "can_toggle": False,
                        "can_run_now": False,
                    }
                    target.update(
                        {
                            "status": st,
                            "last_run_at": item.get("last_run_at"),
                            "last_result": item.get("last_result"),
                            "next_run_at": item.get("next_run_at"),
                            "can_toggle": False,
                            "can_run_now": False,
                        }
                    )
                    by_key[k] = target
    except Exception as exc:
        logger.debug("[DALTON_SYSTEM_JOBS] Host bridge ops jobs unreachable: %s", exc)
    return list(by_key.values())


def _build_home_job_entry(
    spec: dict[str, Any],
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a single ``home`` scope system job entry from its spec and ``automations`` row."""
    name = str(spec["name"])
    meta = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
    kind = str(meta.get("dalton_kind") or name.removeprefix("dalton-").replace("-", "_"))
    title = HOME_JOB_TITLES.get(name, str(spec.get("description") or name))

    row_status = str(row.get("status") or "").strip().lower() if row else "missing"
    if row and row_status == "active":
        status = "on"
    elif not row or row_status in ("paused", "disabled"):
        status = "off"
    else:
        status = "partial"

    row_meta = (row.get("metadata") if row and isinstance(row.get("metadata"), dict) else None) or {}
    paused_by_user = bool(row_meta.get("paused_by_user"))

    last_exec = row.get("last_execution") if row and isinstance(row.get("last_execution"), dict) else None
    exec_ts, last_result = _summarize_last_execution(last_exec, kind=kind)
    last_run_at = _iso_or_none(row.get("last_run_at")) if row else None
    if not last_run_at:
        last_run_at = exec_ts
    next_run_at = _iso_or_none(row.get("next_run_at")) if row and status == "on" else None

    return {
        "key": name,
        "kind": kind,
        "scope": "home",
        "title": title,
        "description": str((row.get("description") if row else None) or spec.get("description") or title),
        "schedule": str((row.get("cron_expression") if row else None) or spec.get("cron_expression") or ""),
        "timezone": str((row.get("timezone") if row else None) or spec.get("timezone") or "Asia/Shanghai"),
        "status": status,
        "enabled": status == "on",
        "paused_by_user": paused_by_user,
        "last_run_at": last_run_at,
        "last_result": last_result,
        "next_run_at": next_run_at,
        "can_toggle": True,
        "can_run_now": True,
        "automation_id": str(row["automation_id"]) if row and row.get("automation_id") else None,
    }


async def _discover_company_workspaces(
    user_id: str,
    automation_rows: list[dict[str, Any]],
) -> list[str]:
    """Return company workspace IDs for ``user_id`` from ``workspaces`` table + existing automations."""
    ws_ids: list[str] = []
    seen: set[str] = set()
    try:
        async with get_db_connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT workspace_id::text, name, metadata
                    FROM workspaces
                    WHERE user_id = %s
                      AND status <> 'deleted'
                    ORDER BY created_at ASC
                    """,
                (user_id,),
            )
            rows = await cur.fetchall()
            for r in rows or []:
                wid = str(r[0] or "").strip()
                wname = str(r[1] or "").strip()
                wmeta = r[2] if isinstance(r[2], dict) else {}
                if not wid or wid in seen:
                    continue
                if is_home_id(user_id, wid) or is_home_row(
                    {"workspace_id": wid, "name": wname, "metadata": wmeta}
                ):
                    continue
                seen.add(wid)
                ws_ids.append(wid)
    except Exception as exc:
        logger.debug("[DALTON_SYSTEM_JOBS] Failed querying company workspaces: %s", exc)

    for r in automation_rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip().lower()
        if not _TICKER_FROM_AUTO_NAME_RE.match(name):
            continue
        wid = str(r.get("workspace_id") or "").strip()
        if wid and wid not in seen and not is_home_id(user_id, wid):
            seen.add(wid)
            ws_ids.append(wid)
    return ws_ids


def _summarize_company_runs(company_rows: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """Compute ``(last_run_at, last_result, next_run_at)`` across a company's automation rows."""
    latest_run_at: str | None = None
    latest_result: str | None = None
    earliest_next_at: str | None = None

    for r in company_rows:
        if not isinstance(r, dict):
            continue
        last_exec = r.get("last_execution") if isinstance(r.get("last_execution"), dict) else None
        exec_ts, exec_res = _summarize_last_execution(last_exec)
        row_last = _iso_or_none(r.get("last_run_at")) or exec_ts
        if row_last and (latest_run_at is None or row_last > latest_run_at):
            latest_run_at = row_last
            latest_result = exec_res or "已运行"
        elif latest_result is None and exec_res:
            latest_result = exec_res

        if str(r.get("status") or "").lower() == "active":
            nxt = _iso_or_none(r.get("next_run_at"))
            if nxt and (earliest_next_at is None or nxt < earliest_next_at):
                earliest_next_at = nxt

    return latest_run_at, latest_result, earliest_next_at


async def _build_company_job_entry(
    user_id: str,
    workspace_id: str,
    automation_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build a ``company`` scope system job entry by reusing ``tracking.get_tracking_status``."""
    try:
        tracking = await get_tracking_status(user_id, workspace_id)
    except ValueError:
        return None
    except Exception as exc:
        logger.debug(
            "[DALTON_SYSTEM_JOBS] Skipping workspace %s in company jobs: %s",
            workspace_id,
            exc,
        )
        return None

    ticker = str(tracking.get("ticker") or "").strip().upper()
    raw_status = str(tracking.get("status") or "disabled")
    if raw_status == "not_applicable" or not ticker:
        return None
    mapped_status = _TRACKING_STATUS_TO_JOB_STATUS.get(raw_status, "partial")

    prefix = f"dalton-{ticker.lower()}-"
    company_rows = [
        r
        for r in automation_rows
        if isinstance(r, dict)
        and str(r.get("name") or "").strip().lower().startswith(prefix)
        and (not r.get("workspace_id") or str(r.get("workspace_id")) == str(workspace_id))
    ]
    last_run_at, last_result, next_run_at = _summarize_company_runs(company_rows)
    if next_run_at is None:
        task_nexts = [
            str(t["next_run_at"])
            for t in (tracking.get("tasks") or [])
            if isinstance(t, dict) and t.get("status") == "active" and t.get("next_run_at")
        ]
        if task_nexts:
            next_run_at = min(task_nexts)

    return {
        "key": f"company:{ticker}",
        "kind": "company_tracking",
        "scope": "company",
        "title": f"{ticker} 自动跟踪",
        "description": str(tracking.get("label") or ""),
        "schedule": "盯盘每 30 分钟 · 内部资料北京时间 11:00 扫描，Core 每交易日、Watch 每周 · 涨跌 ±4% 触发",
        "workspace_id": str(workspace_id),
        "ticker": ticker,
        "tier": tracking.get("tier"),
        "timezone": tracking.get("timezone"),
        "status": mapped_status,
        "tracking_status": raw_status,
        "enabled": mapped_status == "on",
        "paused_by_user": bool(tracking.get("paused_by_user")),
        "last_run_at": last_run_at,
        "last_result": last_result,
        "next_run_at": next_run_at,
        "can_toggle": True,
        "can_run_now": True,
        "missing_tasks": tracking.get("missing_tasks") or [],
        "paused_tasks": tracking.get("paused_tasks") or [],
        "active_tasks": tracking.get("active_tasks") or [],
        "tasks": tracking.get("tasks") or [],
    }


async def _discover_sector_units(user_id: str) -> list[dict[str, Any]]:
    """The user's active sector units with a workspace binding (P3, flag on only)."""
    if not is_coverage_units_enabled():
        return []
    try:
        from src.dalton.database.coverage_units import list_coverage_units

        units = await list_coverage_units(user_id, include_archived=False)
    except Exception as exc:
        logger.debug("[DALTON_SYSTEM_JOBS] sector unit lookup failed: %s", exc)
        return []
    return [
        dict(u)
        for u in units or []
        if str(u.get("kind") or "") == "sector" and u.get("workspace_id")
    ]


def _build_sector_job_entry(
    user_id: str,
    unit: dict[str, Any],
    automation_rows: list[dict[str, Any]],
    tracking: dict[str, Any],
) -> dict[str, Any]:
    """Build a ``sector`` scope system job entry from a sector unit's tracking status."""
    slug = str(unit.get("slug") or "").strip()
    display_name = str(unit.get("display_name") or slug).strip()
    raw_status = str(tracking.get("status") or "disabled")
    mapped_status = _TRACKING_STATUS_TO_JOB_STATUS.get(raw_status, "partial")

    prefix = f"dalton-sector-{slug.lower()}-"
    sector_rows = [
        r
        for r in automation_rows
        if isinstance(r, dict)
        and str(r.get("name") or "").strip().lower().startswith(prefix)
        and (not r.get("workspace_id") or str(r.get("workspace_id")) == str(unit.get("workspace_id")))
    ]
    last_run_at, last_result, next_run_at = _summarize_company_runs(sector_rows)
    if next_run_at is None:
        task_nexts = [
            str(t["next_run_at"])
            for t in (tracking.get("tasks") or [])
            if isinstance(t, dict) and t.get("status") == "active" and t.get("next_run_at")
        ]
        if task_nexts:
            next_run_at = min(task_nexts)

    return {
        "key": f"sector:{slug}",
        "kind": "sector_tracking",
        "scope": "sector",
        "title": f"{display_name} 行业自动跟踪",
        "description": str(tracking.get("label") or ""),
        "schedule": "议程到期每 2 小时（研究时区）· 内部资料每周六 11:00 增量扫描 · 不跑证券探针",
        "workspace_id": str(unit.get("workspace_id")),
        "unit_id": str(unit.get("unit_id") or "") or None,
        "sector_slug": slug,
        "subject_kind": "sector",
        "ticker": None,
        "timezone": tracking.get("timezone"),
        "status": mapped_status,
        "tracking_status": raw_status,
        "enabled": mapped_status == "on",
        "paused_by_user": bool(tracking.get("paused_by_user")),
        "last_run_at": last_run_at,
        "last_result": last_result,
        "next_run_at": next_run_at,
        "can_toggle": True,
        "can_run_now": True,
        "missing_tasks": tracking.get("missing_tasks") or [],
        "paused_tasks": tracking.get("paused_tasks") or [],
        "active_tasks": tracking.get("active_tasks") or [],
        "tasks": tracking.get("tasks") or [],
    }


async def _resolve_sector_unit_for_key(
    user_id: str,
    target: str,
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a sector key's target (slug or unit_id) to one of the user's bound sector units."""
    owned = units if units is not None else await _discover_sector_units(user_id)
    cleaned = str(target or "").strip()
    for u in owned:
        if str(u.get("slug") or "").strip().lower() == cleaned.lower():
            return u
        if str(u.get("unit_id") or "").strip().lower() == cleaned.lower():
            return u
    raise ValueError(f"未找到行业自动跟踪任务: sector:{cleaned}")


def _get_data_tasks_manifest_map() -> dict[str, DataTaskManifestEntry]:
    return load_data_task_manifest().by_job_key()


def _compute_next_data_task_cron_iso(cron_expr: str, tz_name: str) -> str | None:
    try:
        tz = ZoneInfo(tz_name or "UTC")
        base_local = datetime.now(UTC).astimezone(tz)
        next_local = croniter(cron_expr, base_local).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(UTC).isoformat()
    except Exception:
        return None


def _parse_audit_history(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [dict(item) for item in parsed if isinstance(item, Mapping)]
        except Exception:
            pass
    return []


def _default_seed_audit_history(enabled: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "version": 1,
            "prev_version": None,
            "actor_user_id": "system:manifest_seed",
            "changed_at": "2026-11-07T00:00:00Z",
            "action": "seed",
            "changes": {},
            "snapshot": {"enabled": bool(enabled), "notify_routes": []},
        }
    ]


def _find_audit_entry(
    audit_history: list[dict[str, Any]],
    version: int,
) -> dict[str, Any] | None:
    for item in reversed(audit_history):
        try:
            if int(item.get("version") or 0) == int(version):
                return item
        except (TypeError, ValueError):
            continue
    return None


def _summarize_data_task_run(
    run_row: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(run_row, Mapping) or not run_row:
        return None, None, None
    ts = (
        run_row.get("completed_at")
        or run_row.get("started_at")
        or run_row.get("created_at")
        or run_row.get("scheduled_slot_utc")
    )
    ts_str = _iso_or_none(ts)
    st = str(run_row.get("status") or "").strip().lower()
    failure_reason = str(run_row.get("failure_reason") or "").strip()
    exit_code = run_row.get("exit_code")
    if st == "succeeded":
        return ts_str, "成功", st
    if st == "queued":
        return ts_str, "排队中", st
    if st in ("starting", "running"):
        return ts_str, "运行中", st
    if st == "cancelled":
        return ts_str, "已取消", st
    if st == "timed_out":
        return ts_str, (f"超时：{failure_reason[:60]}" if failure_reason else "运行超时"), st
    if st == "skipped_low_memory":
        return ts_str, "宿主机可用内存不足跳过", st
    if st in ("failed", "aborted"):
        if failure_reason:
            return ts_str, f"失败：{failure_reason[:60]}", st
        if exit_code is not None:
            return ts_str, f"失败 (exit={exit_code})", st
        return ts_str, "执行失败", st
    return ts_str, (st or None), (st or None)


def _build_data_job_entry(
    manifest_entry: DataTaskManifestEntry,
    def_row: Mapping[str, Any] | None = None,
    latest_run: Mapping[str, Any] | None = None,
    notify_routes: list[str] | None = None,
) -> dict[str, Any]:
    job_key = manifest_entry.job_key
    task_code = str((def_row or {}).get("task_code") or manifest_entry.task_code)
    title = str((def_row or {}).get("title") or manifest_entry.title)
    batch_no = int((def_row or {}).get("batch_no") or manifest_entry.batch_no)
    runner = str((def_row or {}).get("runner") or manifest_entry.runner)
    cron_expr = str((def_row or {}).get("cron_expr") or manifest_entry.cron_expr)
    tz_name = str((def_row or {}).get("timezone") or manifest_entry.timezone)
    enabled = (
        bool(def_row["enabled"])
        if isinstance(def_row, Mapping) and "enabled" in def_row
        else bool(manifest_entry.enabled)
    )
    paused_by_user = bool((def_row or {}).get("paused_by_user", False))
    notify_enabled = (
        bool(def_row["notify_enabled"])
        if isinstance(def_row, Mapping) and "notify_enabled" in def_row
        else bool(manifest_entry.notify_enabled)
    )
    cos_mode = str((def_row or {}).get("cos_mode") or manifest_entry.cos_mode)
    consecutive_failures = int((def_row or {}).get("consecutive_failures") or 0)
    config_version = int((def_row or {}).get("config_version") or 1)
    active_version = int((def_row or {}).get("active_version") or 1)
    updated_by = (def_row or {}).get("updated_by")
    audit_history = (
        _parse_audit_history(def_row.get("audit_history"))
        if isinstance(def_row, Mapping) and def_row.get("audit_history") is not None
        else _default_seed_audit_history(enabled=enabled)
    )
    if not audit_history:
        audit_history = _default_seed_audit_history(enabled=enabled)

    active_entry = _find_audit_entry(audit_history, active_version)
    can_rollback = bool(
        active_entry is not None and active_entry.get("prev_version") is not None
    )

    if notify_routes is None:
        snap = (active_entry or {}).get("snapshot")
        if isinstance(snap, Mapping) and isinstance(snap.get("notify_routes"), list):
            effective_routes = [
                str(x).strip()
                for x in snap["notify_routes"]
                if isinstance(x, str) and str(x).strip()
            ]
        else:
            effective_routes = []
    else:
        effective_routes = [
            str(x).strip() for x in notify_routes if isinstance(x, str) and str(x).strip()
        ]

    last_run_at, last_result, last_run_status = _summarize_data_task_run(latest_run)
    next_run_at: str | None = (
        _compute_next_data_task_cron_iso(cron_expr, tz_name) if enabled else None
    )

    return {
        "key": f"data:{job_key}",
        "job_key": job_key,
        "task_code": task_code,
        "kind": "data_task",
        "scope": "data",
        "title": title,
        "description": f"公司公共数据任务 {task_code}（批次 {batch_no}，{runner} 执行）",
        "batch_no": batch_no,
        "runner": runner,
        "schedule": cron_expr,
        "timezone": tz_name,
        "status": "on" if enabled else "off",
        "enabled": enabled,
        "paused_by_user": paused_by_user,
        "notify_enabled": notify_enabled,
        "cos_mode": cos_mode,
        "consecutive_failures": consecutive_failures,
        "config_version": config_version,
        "active_version": active_version,
        "updated_by": updated_by,
        "audit_history": audit_history,
        "notify_routes": effective_routes,
        "last_run_at": last_run_at,
        "last_result": last_result,
        "last_run_status": last_run_status,
        "next_run_at": next_run_at,
        "can_toggle": True,
        "can_run_now": bool(enabled),
        "can_rollback": can_rollback,
    }


def _extract_data_routes_from_model_pref(
    model_pref: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    if not isinstance(model_pref, Mapping):
        return {}
    raw_routes = model_pref.get("dalton_notify_routes")
    if not isinstance(raw_routes, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw_routes.items():
        if not isinstance(k, str):
            continue
        cleaned_k = k.strip()
        if cleaned_k.lower().startswith("data:") and isinstance(v, list):
            jk = cleaned_k.split(":", 1)[1].strip().lower()
            if jk:
                out[jk] = [
                    str(x).strip() for x in v if isinstance(x, str) and str(x).strip()
                ]
    return out


async def _read_pm_data_notify_routes_map(
    *,
    conn: Any = None,
) -> dict[str, list[str]]:
    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        return {}
    pm_uid = pm_ids[0]
    try:
        if conn is not None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT model_preference FROM user_preferences WHERE user_id = %s",
                    (pm_uid,),
                )
                row = await cur.fetchone()
                model_pref = (row or {}).get("model_preference") if isinstance(row, Mapping) else None
                return _extract_data_routes_from_model_pref(model_pref)
        prefs = await get_user_preferences(pm_uid)
        model_pref = (prefs or {}).get("model_preference") if isinstance(prefs, Mapping) else None
        return _extract_data_routes_from_model_pref(model_pref)
    except Exception:
        return {}


async def _fetch_data_system_jobs() -> list[dict[str, Any]]:
    """Fetch all 19 ``data`` scope system jobs for internal/admin callers."""
    manifest_entries = load_data_task_manifest().tasks
    defs_by_key: dict[str, dict[str, Any]] = {}
    runs_by_key: dict[str, dict[str, Any]] = {}
    pm_routes_by_key: dict[str, list[str]] = {}

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        job_key, task_code, title, batch_no, runner,
                        cron_expr, timezone, timeout_seconds, no_output_timeout_seconds,
                        cos_mode, notify_enabled, enabled, paused_by_user,
                        consecutive_failures, config_version, active_version,
                        updated_by, audit_history, updated_at
                    FROM dalton_data_task_defs
                    ORDER BY batch_no ASC, task_code ASC, job_key ASC
                    """
                )
                def_rows = await cur.fetchall()
                for d_row in def_rows or []:
                    if isinstance(d_row, Mapping) and d_row.get("job_key"):
                        defs_by_key[str(d_row["job_key"])] = dict(d_row)

                await cur.execute(
                    """
                    SELECT DISTINCT ON (job_key)
                        run_id, job_key, status, exit_code, failure_reason,
                        scheduled_slot_utc, started_at, completed_at, created_at
                    FROM dalton_data_task_runs
                    ORDER BY job_key, COALESCE(completed_at, started_at, created_at) DESC
                    """
                )
                run_rows = await cur.fetchall()
                for r_row in run_rows or []:
                    if isinstance(r_row, Mapping) and r_row.get("job_key"):
                        runs_by_key[str(r_row["job_key"])] = dict(r_row)

            pm_routes_by_key = await _read_pm_data_notify_routes_map(conn=conn)
    except Exception as exc:
        logger.debug("[DALTON_SYSTEM_JOBS] data task DB query fallback to manifest: %s", exc)
        pm_routes_by_key = await _read_pm_data_notify_routes_map()

    out: list[dict[str, Any]] = []
    for entry in manifest_entries:
        jk = entry.job_key
        out.append(
            _build_data_job_entry(
                entry,
                defs_by_key.get(jk),
                runs_by_key.get(jk),
                pm_routes_by_key.get(jk),
            )
        )
    return out


async def _fetch_single_data_system_job(
    job_key: str,
    *,
    conn: Any = None,
    pm_routes_override: list[str] | None = None,
) -> dict[str, Any]:
    manifest_map = _get_data_tasks_manifest_map()
    manifest_entry = manifest_map.get(job_key)
    if manifest_entry is None:
        raise ValueError(f"未知的数据任务键：data:{job_key}")

    def_row: dict[str, Any] | None = None
    latest_run: dict[str, Any] | None = None
    pm_routes: list[str] | None = pm_routes_override

    async def _query_on_conn(c: Any) -> None:
        nonlocal def_row, latest_run, pm_routes
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    job_key, task_code, title, batch_no, runner,
                    cron_expr, timezone, timeout_seconds, no_output_timeout_seconds,
                    cos_mode, notify_enabled, enabled, paused_by_user,
                    consecutive_failures, config_version, active_version,
                    updated_by, audit_history, updated_at
                FROM dalton_data_task_defs
                WHERE job_key = %s
                """,
                (job_key,),
            )
            fetched_def = await cur.fetchone()
            if isinstance(fetched_def, Mapping):
                def_row = dict(fetched_def)

            await cur.execute(
                """
                SELECT
                    run_id, job_key, status, exit_code, failure_reason,
                    scheduled_slot_utc, started_at, completed_at, created_at
                FROM dalton_data_task_runs
                WHERE job_key = %s
                ORDER BY COALESCE(completed_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (job_key,),
            )
            fetched_run = await cur.fetchone()
            if isinstance(fetched_run, Mapping):
                latest_run = dict(fetched_run)

        if pm_routes is None:
            routes_map = await _read_pm_data_notify_routes_map(conn=c)
            pm_routes = routes_map.get(job_key)

    try:
        if conn is not None:
            await _query_on_conn(conn)
        else:
            async with get_db_connection() as db_conn:
                await _query_on_conn(db_conn)
    except Exception as exc:
        logger.debug(
            "[DALTON_SYSTEM_JOBS] single data task query fallback for %s: %s",
            job_key,
            exc,
        )
        if pm_routes is None:
            routes_map = await _read_pm_data_notify_routes_map()
            pm_routes = routes_map.get(job_key)

    return _build_data_job_entry(manifest_entry, def_row, latest_run, pm_routes)


async def _ensure_and_lock_data_task_def_row(
    cur: Any,
    manifest_entry: DataTaskManifestEntry,
) -> dict[str, Any]:
    """Select the ``dalton_data_task_defs`` row ``FOR UPDATE``, seeding from manifest if absent."""
    await cur.execute(
        """
        SELECT
            job_key, task_code, title, batch_no, runner,
            cron_expr, timezone, timeout_seconds, no_output_timeout_seconds,
            cos_mode, notify_enabled, enabled, paused_by_user,
            consecutive_failures, config_version, active_version,
            updated_by, audit_history, updated_at
        FROM dalton_data_task_defs
        WHERE job_key = %s
        FOR UPDATE
        """,
        (manifest_entry.job_key,),
    )
    row = await cur.fetchone()
    if isinstance(row, Mapping):
        return dict(row)

    seed_audit = _default_seed_audit_history(enabled=manifest_entry.enabled)
    await cur.execute(
        """
        INSERT INTO dalton_data_task_defs (
            job_key, task_code, title, batch_no, runner,
            cron_expr, timezone, timeout_seconds, no_output_timeout_seconds,
            command_argv, workdir, required_secrets,
            cos_mode, notify_enabled, enabled, paused_by_user,
            consecutive_failures, config_version, active_version,
            audit_history, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, FALSE,
            0, 1, 1,
            %s, NOW()
        )
        ON CONFLICT (job_key) DO NOTHING
        """,
        (
            manifest_entry.job_key,
            manifest_entry.task_code,
            manifest_entry.title,
            manifest_entry.batch_no,
            manifest_entry.runner,
            manifest_entry.cron_expr,
            manifest_entry.timezone,
            manifest_entry.timeout_seconds,
            manifest_entry.no_output_timeout_seconds,
            Jsonb(list(manifest_entry.command_argv)),
            manifest_entry.workdir,
            Jsonb(list(manifest_entry.required_secrets)),
            manifest_entry.cos_mode,
            manifest_entry.notify_enabled,
            manifest_entry.enabled,
            Jsonb(seed_audit),
        ),
    )
    await cur.execute(
        """
        SELECT
            job_key, task_code, title, batch_no, runner,
            cron_expr, timezone, timeout_seconds, no_output_timeout_seconds,
            cos_mode, notify_enabled, enabled, paused_by_user,
            consecutive_failures, config_version, active_version,
            updated_by, audit_history, updated_at
        FROM dalton_data_task_defs
        WHERE job_key = %s
        FOR UPDATE
        """,
        (manifest_entry.job_key,),
    )
    seeded = await cur.fetchone()
    if not isinstance(seeded, Mapping):
        raise RuntimeError(f"无法初始化数据任务定义行: {manifest_entry.job_key}")
    return dict(seeded)


async def _write_pm_data_task_notify_route_in_tx(
    conn: Any,
    *,
    pm_user_id: str,
    job_key: str,
    notify_routes: list[str],
) -> None:
    """Persist ``dalton_notify_routes["data:<job_key>"]`` onto ``pm_user_id`` inside ``conn``'s transaction."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO users (user_id, created_at, updated_at)
            VALUES (%s, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
            """,
            (pm_user_id,),
        )
    await upsert_user_preferences(
        user_id=pm_user_id,
        model_preference={
            "dalton_notify_routes": {
                f"data:{job_key}": list(notify_routes),
            }
        },
        conn=conn,
    )


async def record_auto_pruned_data_task_routes(
    conn: Any,
    *,
    actor_user_id: str,
    pruned_routes: Mapping[str, tuple[list[str], list[str]]],
) -> None:
    """Append ``action = "auto_prune_target"`` audit entries and bump versions when PM deletes a personal target."""
    if not pruned_routes:
        return
    manifest_map = _get_data_tasks_manifest_map()
    now_iso = datetime.now(UTC).isoformat()
    actor_uid = str(actor_user_id or "").strip()

    normalized_items: list[tuple[str, DataTaskManifestEntry, list[str], list[str]]] = []
    for raw_key, (old_routes, new_routes) in pruned_routes.items():
        jk = str(raw_key).strip().lower()
        if jk.startswith("data:"):
            jk = jk.split(":", 1)[1].strip().lower()
        manifest_entry = manifest_map.get(jk)
        if manifest_entry is not None:
            normalized_items.append(
                (jk, manifest_entry, list(old_routes), list(new_routes))
            )
    normalized_items.sort(key=lambda item: item[0])

    async with conn.cursor(row_factory=dict_row) as cur:
        for jk, manifest_entry, old_routes, new_routes in normalized_items:
            def_row = await _ensure_and_lock_data_task_def_row(cur, manifest_entry)
            old_ver = int(def_row.get("config_version") or 1)
            active_ver = int(def_row.get("active_version") or 1)
            cur_enabled = bool(def_row.get("enabled", False))
            history = _parse_audit_history(def_row.get("audit_history"))
            if not history:
                history = _default_seed_audit_history(enabled=cur_enabled)

            new_ver = old_ver + 1
            entry = {
                "version": new_ver,
                "prev_version": active_ver,
                "actor_user_id": actor_uid,
                "changed_at": now_iso,
                "action": "auto_prune_target",
                "changes": {
                    "notify_routes": {
                        "old": list(old_routes),
                        "new": list(new_routes),
                    }
                },
                "snapshot": {
                    "enabled": cur_enabled,
                    "notify_routes": list(new_routes),
                },
            }
            updated_history = history + [entry]
            await cur.execute(
                """
                UPDATE dalton_data_task_defs
                SET
                    config_version = %s,
                    active_version = %s,
                    updated_by = %s,
                    audit_history = %s,
                    updated_at = NOW()
                WHERE job_key = %s
                """,
                (
                    new_ver,
                    new_ver,
                    actor_uid,
                    Jsonb(updated_history),
                    jk,
                ),
            )


async def rollback_data_task_config(
    actor_user_id: str,
    job_key: str,
) -> dict[str, Any]:
    """Step backward along the ``prev_version`` chain for ``data:<job_key>`` and record a rollback snapshot."""
    actor_uid = str(actor_user_id or "").strip()
    if not actor_uid:
        raise ValueError("actor_user_id is required")

    if await get_user_tier(actor_uid) not in ("admin", "internal"):
        raise PermissionError("仅内部成员可操作公司公共数据任务")

    raw_key = str(job_key or "").strip()
    norm_input = raw_key if raw_key.lower().startswith("data:") else f"data:{raw_key}"
    scope, target = classify_system_job_key(norm_input)
    if scope != "data":
        raise ValueError(f"仅公司公共数据任务支持按版本链回退: {job_key}")

    pm_ids = get_configured_pm_user_ids()
    if not pm_ids:
        raise RuntimeError("未配置首位管理员 DALTON_PM_USER_IDS，无法操作数据任务")
    pm_user_id = pm_ids[0]

    manifest_entry = _get_data_tasks_manifest_map()[target]
    now_iso = datetime.now(UTC).isoformat()

    async with get_db_connection() as conn, conn.transaction():
        locked_prefs = await lock_user_preferences(pm_user_id, conn=conn)
        async with conn.cursor(row_factory=dict_row) as cur:
            def_row = await _ensure_and_lock_data_task_def_row(cur, manifest_entry)
            old_ver = int(def_row.get("config_version") or 1)
            active_ver = int(def_row.get("active_version") or 1)
            old_enabled = bool(def_row.get("enabled", False))
            history = _parse_audit_history(def_row.get("audit_history"))
            if not history:
                history = _default_seed_audit_history(enabled=old_enabled)

            active_entry = _find_audit_entry(history, active_ver)
            if active_entry is None or active_entry.get("prev_version") is None:
                raise ValueError("已处于最早配置版本，无法继续回退")

            target_ver = int(active_entry["prev_version"])
            target_entry = _find_audit_entry(history, target_ver)
            if target_entry is None or not isinstance(target_entry.get("snapshot"), Mapping):
                raise ValueError("已处于最早配置版本，无法继续回退")

            target_snapshot = target_entry["snapshot"]
            restored_enabled = bool(target_snapshot.get("enabled", False))
            restored_routes = [
                str(x).strip()
                for x in (target_snapshot.get("notify_routes") or [])
                if isinstance(x, str) and str(x).strip()
            ]

            stored_routes_map = _extract_data_routes_from_model_pref(
                (locked_prefs or {}).get("model_preference")
                if isinstance(locked_prefs, Mapping)
                else None
            )
            active_snap = active_entry.get("snapshot")
            if target in stored_routes_map:
                old_routes = stored_routes_map[target]
            elif isinstance(active_snap, Mapping) and isinstance(
                active_snap.get("notify_routes"), list
            ):
                old_routes = [
                    str(x).strip()
                    for x in active_snap["notify_routes"]
                    if isinstance(x, str) and str(x).strip()
                ]
            else:
                old_routes = []

            new_ver = old_ver + 1
            rollback_entry = {
                "version": new_ver,
                "prev_version": target_entry.get("prev_version"),
                "restored_from_version": target_ver,
                "actor_user_id": actor_uid,
                "changed_at": now_iso,
                "action": "rollback",
                "changes": {
                    "enabled": {"old": old_enabled, "new": restored_enabled},
                    "notify_routes": {"old": old_routes, "new": restored_routes},
                },
                "snapshot": {
                    "enabled": restored_enabled,
                    "notify_routes": restored_routes,
                },
            }
            updated_history = history + [rollback_entry]

            await cur.execute(
                """
                UPDATE dalton_data_task_defs
                SET
                    enabled = %s,
                    paused_by_user = %s,
                    config_version = %s,
                    active_version = %s,
                    updated_by = %s,
                    audit_history = %s,
                    updated_at = NOW()
                WHERE job_key = %s
                """,
                (
                    restored_enabled,
                    not restored_enabled,
                    new_ver,
                    new_ver,
                    actor_uid,
                    Jsonb(updated_history),
                    target,
                ),
            )

            if not restored_enabled:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_runs
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE job_key = %s AND status = 'queued'
                    """,
                    (target,),
                )

        await _write_pm_data_task_notify_route_in_tx(
            conn,
            pm_user_id=pm_user_id,
            job_key=target,
            notify_routes=restored_routes,
        )
        job_entry = await _fetch_single_data_system_job(
            target,
            conn=conn,
            pm_routes_override=restored_routes,
        )

    await invalidate_user_prefs_cache(pm_user_id)
    return {
        "ok": True,
        "key": f"data:{target}",
        "restored_from_version": target_ver,
        "config_version": new_ver,
        "active_version": new_ver,
        "job": job_entry,
    }


def classify_system_job_key(key: str) -> tuple[str, str]:
    """Classify a system job key into ``(scope, normalized_target)``.

    Returns:
        - ``("home", "<automation_name>")`` for individual Home global tasks
        - ``("host", "conversation-archive")`` for conversation archive
        - ``("ops", "<ops_key>")`` for read-only ops jobs
        - ``("data", "<job_key>")`` for company-wide data tasks
        - ``("company", "<ticker_or_workspace_id>")`` for company tracking jobs
        - ``("sector", "<slug_or_unit_id>")`` for sector tracking jobs (P3)
    """
    cleaned = str(key or "").strip()
    if not cleaned:
        raise ValueError("任务 key 不能为空")
    lower = cleaned.lower()

    if lower in ("home", "home:all", "global"):
        raise ValueError(
            f"不支持批量全局任务别名 `{key}`，请指定单个全局任务 key（如 dalton-data-health）"
        )

    home_names = {str(s["name"]) for s in build_global_director_specs()}
    if lower in home_names:
        return "home", lower
    if lower.startswith("home:"):
        sub = lower.split(":", 1)[1].strip()
        if sub in home_names:
            return "home", sub
        raise ValueError(f"未找到全局任务: {key}")

    if lower in (
        "conversation-archive",
        "dalton-conversation-archive",
        "host:conversation-archive",
        "conversations",
    ):
        return "host", "conversation-archive"

    ops_keys = {item["key"] for item in DEFAULT_OPS_JOBS}
    if lower in ops_keys or lower.startswith(("ops-", "ops:")):
        norm_ops = lower.replace("ops:", "ops-")
        return "ops", norm_ops

    if lower.startswith("data:"):
        job_key = cleaned.split(":", 1)[1].strip().lower()
        manifest_map = _get_data_tasks_manifest_map()
        if not job_key or job_key not in manifest_map:
            raise ValueError(f"未知的数据任务键: {key}")
        return "data", job_key

    # 行业单元的 key 必须带 sector: 前缀：裸 slug 会落进公司分支、被当成
    # ticker 去解析（行业没有 ticker，P3 禁止从名称猜）。
    if lower.startswith("sector:"):
        target = cleaned.split(":", 1)[1].strip()
        if not target:
            raise ValueError("行业任务 key 缺少 slug 或 unit_id")
        return "sector", target

    if lower.startswith("company:"):
        target = cleaned.split(":", 1)[1].strip()
        if not target:
            raise ValueError("公司任务 key 缺少股票代码或工作区 ID")
        return "company", target

    return "company", cleaned


def _apply_caller_visibility(job: dict[str, Any], *, is_pm: bool) -> dict[str, Any]:
    """Adjust ``can_toggle``, ``can_run_now``, and ``log_tail`` visibility for non-PM callers."""
    is_self_earnings_cal = (
        job.get("scope") == "home"
        and str(job.get("key") or "").strip().lower() == "dalton-earnings-calendar"
    )
    if not is_pm and job.get("scope") in ("home", "host") and not is_self_earnings_cal:
        job["can_toggle"] = False
        job["can_run_now"] = False
    if not is_pm and job.get("scope") == "host":
        job.pop("log_tail", None)
    return job


async def list_system_jobs(user_id: str) -> dict[str, Any]:
    """Return the unified manifest of all system jobs across ``company``, ``home``, ``host``, ``ops``, and ``data``."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    pm = is_pm_user(uid)
    is_internal = await get_user_tier(uid) in ("admin", "internal")
    rows = await list_all_user_automations(uid, name_prefix="dalton-")
    by_name: dict[str, dict[str, Any]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("name"):
            r_name = str(r["name"]).strip()
            if r_name not in by_name:
                by_name[r_name] = r

    company_ws_ids = await _discover_company_workspaces(uid, rows)
    company_jobs: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for wid in company_ws_ids:
        entry = await _build_company_job_entry(uid, wid, rows)
        if entry and entry["ticker"] not in seen_tickers:
            seen_tickers.add(entry["ticker"])
            company_jobs.append(entry)

    home_specs = build_global_director_specs()
    home_jobs = [
        _apply_caller_visibility(_build_home_job_entry(spec, by_name.get(str(spec["name"]))), is_pm=pm)
        for spec in home_specs
    ]

    # 行业单元（P3）：开关关闭时 _discover_sector_units 直接返回空列表，
    # 不查注册表，输出与 P3 前一致（无 sector 组）。
    sector_units = await _discover_sector_units(uid)
    sector_jobs: list[dict[str, Any]] = []
    for s_unit in sector_units:
        s_ws = str(s_unit.get("workspace_id"))
        try:
            s_tracking = await get_tracking_status(uid, s_ws)
        except ValueError:
            continue
        if str(s_tracking.get("subject_kind") or "") != "sector":
            continue
        sector_jobs.append(_build_sector_job_entry(uid, s_unit, rows, s_tracking))

    host_job, ops_jobs = await asyncio.gather(
        _fetch_host_conversation_archive_job(),
        _fetch_ops_jobs(),
    )
    host_jobs = [_apply_caller_visibility(host_job, is_pm=pm)]

    data_jobs: list[dict[str, Any]] = []
    if is_internal:
        data_jobs = await _fetch_data_system_jobs()

    all_jobs = [*company_jobs, *sector_jobs, *home_jobs, *host_jobs, *ops_jobs, *data_jobs]
    groups: dict[str, list[dict[str, Any]]] = {
        "company": company_jobs,
        "home": home_jobs,
        "host": host_jobs,
        "ops": ops_jobs,
    }
    if sector_units:
        groups["sector"] = sector_jobs
    if is_internal:
        groups["data"] = data_jobs
    return {
        "ok": True,
        "is_pm": pm,
        "jobs": all_jobs,
        "groups": groups,
    }


async def _resolve_company_workspace_id(user_id: str, target: str) -> str:
    """Resolve a ticker or workspace_id string into a workspace_id for ``user_id``."""
    cleaned = str(target or "").strip()
    if "-" in cleaned and len(cleaned) >= 32:
        return cleaned
    resolved = await resolve_workspace_id_by_ticker(cleaned.upper(), user_id)
    if resolved:
        return resolved
    return cleaned


async def get_system_job_status(user_id: str, key: str) -> dict[str, Any]:
    """Return the status item for a single system job ``key``."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    pm = is_pm_user(uid)
    scope, target = classify_system_job_key(key)
    if scope == "data":
        if await get_user_tier(uid) not in ("admin", "internal"):
            raise PermissionError("仅内部成员可查看公司公共数据任务")
        job = await _fetch_single_data_system_job(target)
        return {"ok": True, "job": job}

    if scope == "host":
        job = _apply_caller_visibility(await _fetch_host_conversation_archive_job(), is_pm=pm)
        return {"ok": True, "job": job}

    if scope == "ops":
        ops_jobs = await _fetch_ops_jobs()
        for item in ops_jobs:
            if item["key"] == target:
                return {"ok": True, "job": item}
        raise ValueError(f"未找到运维任务: {key}")

    rows = await list_all_user_automations(uid, name_prefix="dalton-")
    if scope == "home":
        specs = build_global_director_specs()
        by_name = {
            str(r.get("name") or "").strip(): r
            for r in rows
            if isinstance(r, dict) and r.get("name")
        }
        spec = next((s for s in specs if str(s["name"]) == target), None)
        if not spec:
            raise ValueError(f"未找到全局任务: {key}")
        job = _apply_caller_visibility(_build_home_job_entry(spec, by_name.get(target)), is_pm=pm)
        return {"ok": True, "job": job}

    if scope == "sector":
        s_unit = await _resolve_sector_unit_for_key(uid, target)
        s_ws = str(s_unit.get("workspace_id"))
        s_tracking = await get_tracking_status(uid, s_ws)
        if str(s_tracking.get("subject_kind") or "") != "sector":
            raise ValueError(f"未找到行业自动跟踪任务: {key}")
        return {"ok": True, "job": _build_sector_job_entry(uid, s_unit, rows, s_tracking)}

    ws_id = await _resolve_company_workspace_id(uid, target)
    entry = await _build_company_job_entry(uid, ws_id, rows)
    if not entry:
        raise ValueError(f"未找到公司自动跟踪任务: {key}")
    return {"ok": True, "job": entry}


async def _enable_single_home_job(
    user_id: str,
    spec: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    force: bool = True,
    respect_user_pause: bool = False,
    conn: Any = None,
) -> None:
    if existing_row and not force and respect_user_pause:
        r_meta = existing_row.get("metadata") if isinstance(existing_row.get("metadata"), dict) else {}
        if bool(r_meta.get("paused_by_user")):
            return

    spec_copy = dict(spec)
    spec_meta = dict(spec_copy.get("metadata") or {})
    spec_meta["paused_by_user"] = None
    spec_copy["metadata"] = spec_meta
    spec_copy["status"] = "active"
    await schedule_dalton_automation_helper(
        str(spec_copy["name"]),
        spec_copy,
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
        action="upsert",
        conn=conn,
    )


async def _disable_single_home_job(
    user_id: str,
    spec: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    conn: Any = None,
) -> None:
    conn_kw = {"conn": conn} if conn is not None else {}
    if existing_row and existing_row.get("automation_id"):
        new_meta = dict(existing_row.get("metadata") or {})
        new_meta["paused_by_user"] = True
        await auto_db.update_automation(
            str(existing_row["automation_id"]),
            user_id,
            status="paused",
            next_run_at=None,
            metadata=new_meta,
            **conn_kw,
        )
        return

    # Create directly in paused state with paused_by_user=True so user choice is persisted
    spec_copy = dict(spec)
    spec_meta = dict(spec_copy.get("metadata") or {})
    spec_meta["paused_by_user"] = True
    spec_copy["metadata"] = spec_meta
    spec_copy["status"] = "paused"
    await schedule_dalton_automation_helper(
        str(spec_copy["name"]),
        spec_copy,
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
        action="upsert",
        conn=conn,
    )


async def _create_paused_home_job_for_run_now(
    user_id: str,
    spec: dict[str, Any],
    *,
    conn: Any = None,
) -> None:
    """Create a missing Home automation directly in ``paused`` state without ``paused_by_user``."""
    spec_copy = dict(spec)
    spec_meta = dict(spec_copy.get("metadata") or {})
    spec_meta.pop("paused_by_user", None)
    spec_copy["metadata"] = spec_meta
    spec_copy["status"] = "paused"
    await schedule_dalton_automation_helper(
        str(spec_copy["name"]),
        spec_copy,
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
        action="upsert",
        conn=conn,
    )


async def toggle_system_job(
    user_id: str,
    key: str,
    enabled: bool | None = None,
    *,
    notify_routes: list[str] | None = None,
    actor_user_id: str | None = None,
    force: bool = True,
    respect_user_pause: bool = False,
) -> dict[str, Any]:
    """Enable (upsert/resume) or disable (pause with ``paused_by_user``) a system job."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    scope, target = classify_system_job_key(key)
    if scope == "ops":
        raise PermissionError("运维任务为只读，不支持在系统内开关")

    if scope == "data":
        actor_uid = str(actor_user_id if actor_user_id is not None else uid).strip()
        if not actor_uid or await get_user_tier(actor_uid) not in ("admin", "internal"):
            raise PermissionError("仅内部成员可操作公司公共数据任务")
        if enabled is None and notify_routes is None:
            raise ValueError("请至少提供 enabled 或 notify_routes")
        if notify_routes is not None and not isinstance(notify_routes, list):
            raise ValueError("notify_routes 必须为字符串列表")
        pm_ids = get_configured_pm_user_ids()
        if not pm_ids:
            raise RuntimeError("未配置首位管理员 DALTON_PM_USER_IDS，无法操作数据任务")
        pm_user_id = pm_ids[0]
        manifest_entry = _get_data_tasks_manifest_map()[target]
        now_iso = datetime.now(UTC).isoformat()
        route_key = f"data:{target}"
        submitted_route_map = (
            {route_key: list(notify_routes)} if notify_routes is not None else None
        )

        async def _read_unlocked_pm_routes() -> Mapping[str, Any] | None:
            unlocked = await get_user_preferences(pm_user_id)
            unlocked_mp = (
                (unlocked or {}).get("model_preference")
                if isinstance(unlocked, Mapping)
                else None
            )
            routes = (
                unlocked_mp.get(DALTON_NOTIFY_ROUTES_KEY)
                if isinstance(unlocked_mp, Mapping)
                else None
            )
            return routes if isinstance(routes, Mapping) else None

        async def _run_data_toggle_in_lock(
            prefetched_groups: Any,
            expected_stored_routes: dict[str, list[str]] | None,
        ) -> tuple[bool, list[str], int, dict[str, Any], list[str] | None]:
            normalized_routes_local: list[str] | None = None
            async with get_db_connection() as conn, conn.transaction():
                locked_prefs = await lock_user_preferences(pm_user_id, conn=conn)
                locked_model_pref = (
                    dict(locked_prefs.get("model_preference") or {})
                    if isinstance(locked_prefs, Mapping)
                    and isinstance(locked_prefs.get("model_preference"), Mapping)
                    else {}
                )
                stored_routes_raw = (
                    locked_model_pref.get(DALTON_NOTIFY_ROUTES_KEY)
                    if isinstance(
                        locked_model_pref.get(DALTON_NOTIFY_ROUTES_KEY), Mapping
                    )
                    else {}
                )
                if notify_routes is not None:
                    patch_model_pref: dict[str, Any] = {
                        DALTON_NOTIFY_ROUTES_KEY: {route_key: list(notify_routes)}
                    }
                    try:
                        validate_dalton_model_preferences(
                            patch_model_pref,
                            effective_model_preference={
                                **locked_model_pref,
                                **patch_model_pref,
                            },
                            stored_model_preference=locked_model_pref,
                        )
                    except TuningError as exc:
                        raise ValueError(str(exc)) from exc
                    await validate_dalton_notify_routes_for_user(
                        actor_uid,
                        {route_key: list(notify_routes)},
                        stored_routes_raw,
                        discovered_groups=prefetched_groups,
                        expected_stored_routes=expected_stored_routes,
                    )
                    normalized_routes_local = list(
                        (patch_model_pref.get(DALTON_NOTIFY_ROUTES_KEY) or {}).get(
                            route_key
                        )
                        or []
                    )

                async with conn.cursor(row_factory=dict_row) as cur:
                    def_row = await _ensure_and_lock_data_task_def_row(
                        cur, manifest_entry
                    )
                    old_ver = int(def_row.get("config_version") or 1)
                    active_ver = int(def_row.get("active_version") or 1)
                    old_enabled = bool(def_row.get("enabled", False))
                    history = _parse_audit_history(def_row.get("audit_history"))
                    if not history:
                        history = _default_seed_audit_history(enabled=old_enabled)

                    stored_routes_map = _extract_data_routes_from_model_pref(
                        locked_model_pref
                    )
                    active_entry = _find_audit_entry(history, active_ver)
                    active_snap = (active_entry or {}).get("snapshot")
                    if target in stored_routes_map:
                        old_routes = stored_routes_map[target]
                    elif isinstance(active_snap, Mapping) and isinstance(
                        active_snap.get("notify_routes"), list
                    ):
                        old_routes = [
                            str(x).strip()
                            for x in active_snap["notify_routes"]
                            if isinstance(x, str) and str(x).strip()
                        ]
                    else:
                        old_routes = []

                    new_enabled = (
                        bool(enabled) if enabled is not None else old_enabled
                    )
                    new_routes = (
                        list(normalized_routes_local)
                        if normalized_routes_local is not None
                        else list(old_routes)
                    )

                    changes: dict[str, Any] = {}
                    if enabled is not None or new_enabled != old_enabled:
                        changes["enabled"] = {
                            "old": old_enabled,
                            "new": new_enabled,
                        }
                    if normalized_routes_local is not None or new_routes != old_routes:
                        changes["notify_routes"] = {
                            "old": old_routes,
                            "new": new_routes,
                        }

                    new_ver = old_ver + 1
                    audit_entry = {
                        "version": new_ver,
                        "prev_version": active_ver,
                        "actor_user_id": actor_uid,
                        "changed_at": now_iso,
                        "action": "update",
                        "changes": changes,
                        "snapshot": {
                            "enabled": new_enabled,
                            "notify_routes": new_routes,
                        },
                    }
                    updated_history = history + [audit_entry]

                    await cur.execute(
                        """
                        UPDATE dalton_data_task_defs
                        SET
                            enabled = %s,
                            paused_by_user = %s,
                            config_version = %s,
                            active_version = %s,
                            updated_by = %s,
                            audit_history = %s,
                            updated_at = NOW()
                        WHERE job_key = %s
                        """,
                        (
                            new_enabled,
                            not new_enabled,
                            new_ver,
                            new_ver,
                            actor_uid,
                            Jsonb(updated_history),
                            target,
                        ),
                    )

                    if not new_enabled:
                        await cur.execute(
                            """
                            UPDATE dalton_data_task_runs
                            SET status = 'cancelled', updated_at = NOW()
                            WHERE job_key = %s AND status = 'queued'
                            """,
                            (target,),
                        )

                if normalized_routes_local is not None:
                    await _write_pm_data_task_notify_route_in_tx(
                        conn,
                        pm_user_id=pm_user_id,
                        job_key=target,
                        notify_routes=new_routes,
                    )

                job_entry = await _fetch_single_data_system_job(
                    target,
                    conn=conn,
                    pm_routes_override=new_routes,
                )
                return (
                    new_enabled,
                    new_routes,
                    new_ver,
                    job_entry,
                    normalized_routes_local,
                )

        new_enabled, new_routes, new_ver, job_entry, normalized_routes = (
            await run_with_feishu_group_prefetch(
                user_id=actor_uid,
                submitted_routes=submitted_route_map,
                read_unlocked_routes=_read_unlocked_pm_routes,
                run_in_lock=_run_data_toggle_in_lock,
            )
        )
        if normalized_routes is not None:
            await invalidate_user_prefs_cache(pm_user_id)
        return {
            "ok": True,
            "key": f"data:{target}",
            "enabled": new_enabled,
            "notify_routes": new_routes,
            "config_version": new_ver,
            "active_version": new_ver,
            "job": job_entry,
        }

    if notify_routes is not None:
        raise ValueError("仅公司公共数据任务支持通过此接口修改 notify_routes")
    if enabled is None:
        raise ValueError("enabled 不能为空")

    if scope == "host" or (scope == "home" and target != "dalton-earnings-calendar"):
        require_pm_user(uid)

    if scope == "host":
        action_segment = "enable" if enabled else "disable"
        status_code, data = await _call_host_bridge(
            "POST", f"/jobs/conversation-archive/{action_segment}"
        )
        if status_code != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            detail = (data or {}).get("error") or f"宿主机桥接返回 HTTP {status_code}"
            raise RuntimeError(f"切换团队对话归档状态失败: {detail}")
        job = await _fetch_host_conversation_archive_job()
        return {"ok": True, "job": job}

    if scope == "home":
        specs = build_global_director_specs()
        spec = next((s for s in specs if str(s["name"]) == target), None)
        if not spec:
            raise ValueError(f"未找到全局任务: {key}")

        async with user_automations_lock(uid) as conn:
            rows = await list_all_user_automations(
                uid,
                name_prefix=target,
                conn=conn,
            )
            existing_row = next(
                (r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip() == target),
                None,
            )
            paused_by_user = bool(
                existing_row
                and isinstance(existing_row.get("metadata"), dict)
                and existing_row["metadata"].get("paused_by_user")
            )
            if enabled and not force and respect_user_pause and paused_by_user:
                return {"ok": True, "job": _build_home_job_entry(spec, existing_row)}

            if enabled:
                await _enable_single_home_job(
                    uid,
                    spec,
                    existing_row,
                    force=force,
                    respect_user_pause=respect_user_pause,
                    conn=conn,
                )
            else:
                await _disable_single_home_job(
                    uid,
                    spec,
                    existing_row,
                    conn=conn,
                )
            updated_rows = await list_all_user_automations(
                uid,
                name_prefix=target,
                conn=conn,
            )
            updated_row = next(
                (r for r in updated_rows if isinstance(r, dict) and str(r.get("name") or "").strip() == target),
                None,
            )
            return {"ok": True, "job": _build_home_job_entry(spec, updated_row)}

    if scope == "sector":
        s_unit = await _resolve_sector_unit_for_key(uid, target)
        s_ws = str(s_unit.get("workspace_id"))
        if enabled:
            await enable_tracking(uid, s_ws, force=force, respect_user_pause=respect_user_pause)
        else:
            await disable_tracking(uid, s_ws)
        rows_after = await list_all_user_automations(uid, name_prefix="dalton-")
        s_tracking = await get_tracking_status(uid, s_ws)
        if str(s_tracking.get("subject_kind") or "") != "sector":
            raise ValueError(f"未找到行业自动跟踪任务: {key}")
        return {"ok": True, "job": _build_sector_job_entry(uid, s_unit, rows_after, s_tracking)}

    ws_id = await _resolve_company_workspace_id(uid, target)
    if enabled:
        await enable_tracking(
            uid,
            ws_id,
            force=force,
            respect_user_pause=respect_user_pause,
        )
    else:
        await disable_tracking(uid, ws_id)
    rows_after = await list_all_user_automations(uid, name_prefix="dalton-")
    entry = await _build_company_job_entry(uid, ws_id, rows_after)
    if not entry:
        raise ValueError(f"未找到公司自动跟踪任务: {key}")
    return {"ok": True, "job": entry}


async def _trigger_automation_row_async(user_id: str, automation_row: dict[str, Any]) -> dict[str, Any]:
    """Trigger an automation row immediately via ``AutomationScheduler`` without altering its schedule."""
    auto_id = str(automation_row["automation_id"])
    try:
        res = await auto_lifecycle.trigger_automation(auto_id, user_id)
        return {
            "execution_id": res.get("execution_id"),
            "automation_id": auto_id,
            "status": "triggered",
        }
    except ValueError as exc:
        if "Automation not found" not in str(exc):
            raise
        logger.warning("[DALTON_SYSTEM_JOBS] trigger_automation fallback for %s: %s", auto_id, exc)
        scheduler = AutomationScheduler.get_instance()
        execution_id = await exec_db.create_execution(
            automation_id=auto_id,
            scheduled_at=datetime.now(UTC),
            server_id=scheduler.server_id,
        )
        scheduler.dispatch(automation_row, execution_id, name=f"manual_exec_{auto_id[:8]}")
        return {
            "execution_id": execution_id,
            "automation_id": auto_id,
            "status": "triggered",
        }


async def run_system_job_now(
    user_id: str,
    key: str,
    *,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Trigger an immediate asynchronous execution of a system job."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    scope, target = classify_system_job_key(key)
    if scope == "ops":
        raise PermissionError("运维任务为只读，不支持立即运行")

    if scope == "data":
        actor_uid = str(actor_user_id if actor_user_id is not None else uid).strip()
        if not actor_uid or await get_user_tier(actor_uid) not in ("admin", "internal"):
            raise PermissionError("仅内部成员可操作公司公共数据任务")
        pm_ids = get_configured_pm_user_ids()
        if not pm_ids:
            raise RuntimeError("未配置首位管理员 DALTON_PM_USER_IDS，无法操作数据任务")
        manifest_entry = _get_data_tasks_manifest_map()[target]
        now_utc = datetime.now(UTC)

        async with get_db_connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT job_key, title, enabled, timeout_seconds
                    FROM dalton_data_task_defs
                    WHERE job_key = %s
                    FOR UPDATE
                    """,
                    (target,),
                )
                def_row = await cur.fetchone()
                if not isinstance(def_row, Mapping) or not bool(def_row.get("enabled", False)):
                    raise ValueError("任务已关闭，请先开启后再手动运行")

                timeout_s = max(60, int(def_row.get("timeout_seconds") or manifest_entry.timeout_seconds or 1800))
                deadline_utc = now_utc + timedelta(seconds=timeout_s)
                title = str(def_row.get("title") or manifest_entry.title)
                new_run_id = str(uuid4())

                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id,
                        job_key,
                        trigger_source,
                        triggered_by_user_id,
                        scheduled_slot_utc,
                        deadline_utc,
                        status
                    )
                    VALUES (%s::uuid, %s, 'manual', %s, %s, %s, 'queued')
                    ON CONFLICT (job_key) WHERE status = 'queued' DO NOTHING
                    RETURNING run_id
                    """,
                    (new_run_id, target, actor_uid, now_utc, deadline_utc),
                )
                inserted = await cur.fetchone()
                if isinstance(inserted, Mapping) and inserted.get("run_id"):
                    run_id = str(inserted["run_id"])
                    run_status = "queued"
                else:
                    await cur.execute(
                        """
                        SELECT run_id
                        FROM dalton_data_task_runs
                        WHERE job_key = %s AND status = 'queued'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (target,),
                    )
                    existing_queued = await cur.fetchone()
                    run_id = (
                        str(existing_queued["run_id"])
                        if isinstance(existing_queued, Mapping) and existing_queued.get("run_id")
                        else ""
                    )
                    run_status = "already_queued"

        where_hint = "设置 → 自动任务 → 数据任务面板"
        return {
            "ok": True,
            "key": f"data:{target}",
            "job_key": target,
            "scope": "data",
            "async": True,
            "status": run_status,
            "run_id": run_id,
            "triggered_by_user_id": actor_uid,
            "where_to_check": where_hint,
            "message": f"已将「{title}」加入执行队列（run_id={run_id}），将由后台调度器异步执行，可在「{where_hint}」查看结果。",
        }

    if scope == "host" or (scope == "home" and target != "dalton-earnings-calendar"):
        require_pm_user(uid)

    if scope == "host":
        status_code, data = await _call_host_bridge(
            "POST", "/jobs/conversation-archive/run", timeout_s=10.0
        )
        if status_code in (200, 202) and isinstance(data, dict) and data.get("ok") is True:
            run_status = str(data.get("status") or "started")
            already_running = run_status == "already_running" or bool(data.get("already_running"))
            msg = (
                "团队对话归档任务已在后台运行中（跳过重复启动），可在「设置 → 自动任务」面板查看运行状态与日志尾部。"
                if already_running
                else "已异步启动宿主机团队对话归档任务，可在「设置 → 自动任务」面板查看最新运行结果与日志尾部。"
            )
            return {
                "ok": True,
                "key": "conversation-archive",
                "scope": "host",
                "async": True,
                "status": run_status,
                "already_running": already_running,
                "pid": data.get("pid"),
                "where_to_check": "设置 → 自动任务 → 宿主机（团队对话归档）",
                "message": msg,
            }
        detail = (data or {}).get("error") or f"宿主机桥接返回 HTTP {status_code}"
        raise RuntimeError(f"启动团队对话归档失败: {detail}")

    if scope == "home":
        specs = build_global_director_specs()
        spec = next((s for s in specs if str(s["name"]) == target), None)
        if not spec:
            raise ValueError(f"未找到可立即运行的全局任务: {key}")

        async with user_automations_lock(uid) as conn:
            rows = await list_all_user_automations(
                uid,
                name_prefix=target,
                conn=conn,
            )
            row = next(
                (r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip() == target),
                None,
            )
            if row is None:
                # Create the row paused without paused_by_user so running once does not silently enable cron or mark user-paused
                await _create_paused_home_job_for_run_now(uid, spec, conn=conn)
                rows = await list_all_user_automations(
                    uid,
                    name_prefix=target,
                    conn=conn,
                )
                row = next(
                    (r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip() == target),
                    None,
                )
        if row is None or not row.get("automation_id"):
            raise RuntimeError(f"无法准备全局任务实例: {target}")

        trig = await _trigger_automation_row_async(uid, row)
        title = HOME_JOB_TITLES.get(target, target)
        where_hint = (
            "设置 → 自动任务 / 数据源地图面板"
            if target == "dalton-data-health"
            else "设置 → 自动任务 或 Home 工作区对话记录"
        )
        return {
            "ok": True,
            "key": target,
            "scope": "home",
            "async": True,
            "status": "triggered",
            "automation_id": trig["automation_id"],
            "execution_id": trig["execution_id"],
            "where_to_check": where_hint,
            "message": f"已异步触发「{title}」（execution_id={trig['execution_id']}），请稍后在「{where_hint}」查看执行结果。",
        }

    # Sector scope: trigger the sector unit's agenda automation if it exists (P3)
    if scope == "sector":
        s_unit = await _resolve_sector_unit_for_key(uid, target)
        s_slug = str(s_unit.get("slug") or "").strip()
        s_ws = str(s_unit.get("workspace_id"))
        agenda_name = f"dalton-sector-{s_slug.lower()}-agenda"
        rows = await list_all_user_automations(uid, name_prefix=agenda_name)
        row = next(
            (
                r
                for r in rows
                if isinstance(r, dict)
                and str(r.get("name") or "").strip().lower() == agenda_name
                and (not r.get("workspace_id") or str(r.get("workspace_id")) == str(s_ws))
            ),
            None,
        )
        if row is None or not row.get("automation_id"):
            raise ValueError("该行业未开启自动跟踪，请先开启")
        trig = await _trigger_automation_row_async(uid, row)
        s_name = str(s_unit.get("display_name") or s_slug)
        where_hint = f"{s_name} 行业工作区或「设置 → 自动任务」面板"
        return {
            "ok": True,
            "key": f"sector:{s_slug}",
            "scope": "sector",
            "async": True,
            "status": "triggered",
            "automation_id": trig["automation_id"],
            "execution_id": trig["execution_id"],
            "where_to_check": where_hint,
            "message": f"已异步触发 {s_name} 行业议程检查（execution_id={trig['execution_id']}），请稍后在{where_hint}查看运行结果。",
        }

    # Company scope: trigger the company's watch automation (`dalton-<t>-watch`) if it exists
    ws_id = await _resolve_company_workspace_id(uid, target)
    tracking = await get_tracking_status(uid, ws_id)
    ticker = str(tracking.get("ticker") or "").strip().upper()
    if tracking.get("status") == "not_applicable" or not ticker:
        raise ValueError(f"Unable to resolve company ticker for workspace '{ws_id}'")
    watch_name = f"dalton-{ticker.lower()}-watch"
    rows = await list_all_user_automations(uid, name_prefix=watch_name)
    row = next(
        (
            r
            for r in rows
            if isinstance(r, dict)
            and str(r.get("name") or "").strip().lower() == watch_name
            and (not r.get("workspace_id") or str(r.get("workspace_id")) == str(ws_id))
        ),
        None,
    )
    if row is None or not row.get("automation_id"):
        raise ValueError("该公司未开启自动跟踪，请先开启")

    trig = await _trigger_automation_row_async(uid, row)
    where_hint = f"{ticker} 公司工作区或「设置 → 自动任务」面板"
    return {
        "ok": True,
        "key": f"company:{ticker}",
        "ticker": ticker,
        "workspace_id": ws_id,
        "scope": "company",
        "async": True,
        "status": "triggered",
        "automation_id": trig["automation_id"],
        "execution_id": trig["execution_id"],
        "where_to_check": where_hint,
        "message": f"已异步触发 {ticker} 自动盯盘检查（execution_id={trig['execution_id']}），请稍后在{where_hint}查看运行结果。",
    }
