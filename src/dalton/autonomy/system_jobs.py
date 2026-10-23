# ruff: noqa: BLE001
"""Unified Dalton system jobs service (company, home, host, ops)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.dalton.autonomy.schedule import (
    build_global_director_specs,
    list_all_user_automations,
    schedule_dalton_automation_helper,
    user_automations_lock,
)
from src.dalton.autonomy.tracking import (
    _TICKER_FROM_AUTO_NAME_RE,
    disable_tracking,
    enable_tracking,
    get_tracking_status,
    resolve_workspace_id_by_ticker,
)
from src.dalton.settings import (
    get_dalton_bridge_base_url,
    get_dalton_bridge_token,
    get_dalton_config,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.home_workspace import is_home_id, is_home_row
from src.server.database.pool import get_db_connection
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
        "key": "ops-log-rotate",
        "kind": "ops_log_rotate",
        "title": "日志轮转",
        "description": "宿主机 launchd 定时清理与归档运行日志",
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
    token = get_dalton_bridge_token()
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
        "description": "每日 21:15 在宿主机增量抽取 OpenClaw 团队对话并写入投研记忆库",
        "schedule": "每日 21:15",
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

    ticker = str(tracking.get("ticker") or "").upper()
    raw_status = str(tracking.get("status") or "disabled")
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
        "schedule": "盯盘每 30 分钟 · 每日增量 21:30 · 涨跌 ±4% 触发",
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


def classify_system_job_key(key: str) -> tuple[str, str]:
    """Classify a system job key into ``(scope, normalized_target)``.

    Returns:
        - ``("home", "<automation_name>")`` for individual Home global tasks
        - ``("host", "conversation-archive")`` for conversation archive
        - ``("ops", "<ops_key>")`` for read-only ops jobs
        - ``("company", "<ticker_or_workspace_id>")`` for company tracking jobs
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
    """Return the unified manifest of all system jobs across ``company``, ``home``, ``host``, and ``ops``."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    pm = is_pm_user(uid)
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

    host_job, ops_jobs = await asyncio.gather(
        _fetch_host_conversation_archive_job(),
        _fetch_ops_jobs(),
    )
    host_jobs = [_apply_caller_visibility(host_job, is_pm=pm)]

    all_jobs = [*company_jobs, *home_jobs, *host_jobs, *ops_jobs]
    return {
        "ok": True,
        "is_pm": pm,
        "jobs": all_jobs,
        "groups": {
            "company": company_jobs,
            "home": home_jobs,
            "host": host_jobs,
            "ops": ops_jobs,
        },
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
    enabled: bool,
    *,
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


async def run_system_job_now(user_id: str, key: str) -> dict[str, Any]:
    """Trigger an immediate asynchronous execution of a system job."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")

    scope, target = classify_system_job_key(key)
    if scope == "ops":
        raise PermissionError("运维任务为只读，不支持立即运行")

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

    # Company scope: trigger the company's watch automation (`dalton-<t>-watch`) if it exists
    ws_id = await _resolve_company_workspace_id(uid, target)
    tracking = await get_tracking_status(uid, ws_id)
    ticker = str(tracking["ticker"])
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
