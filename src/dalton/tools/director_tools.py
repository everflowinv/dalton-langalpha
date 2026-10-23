# ruff: noqa: BLE001
"""Research Director & automation scheduling tools for Dalton (PK.15, PK.19)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.auth import verify_workspace_access
from src.dalton.autonomy.board import build_coverage_board
from src.dalton.autonomy.schedule import (
    schedule_dalton_automation_helper,
)
from src.dalton.autonomy.tracking import (
    _resolve_workspace_ticker,
    disable_tracking,
    enable_tracking,
    get_tracking_status,
    resolve_workspace_id_by_ticker,
)
from src.dalton.database import plans as plans_db
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)


def _render_daily_plan_markdown(
    *,
    plan_date: str,
    budget: dict[str, Any],
    dispatches: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    pm_attention: list[dict[str, Any]],
) -> str:
    cap = float(budget.get("cap_usd") or budget.get("daily_cap_usd") or 0.0)
    spent = float(budget.get("spent_usd") or budget.get("spent_today_usd") or 0.0)
    rem = float(budget.get("remaining_usd") or 0.0)
    res = float(budget.get("reserve_for_events_usd") or 0.0)
    planned = sum(float(d.get("est_usd") or 0.0) for d in dispatches if isinstance(d, dict))

    lines = [
        f"# Dalton 研究总监每日调度计划（{plan_date}）",
        "",
        "## 1. 今日预算与额度快照",
        "",
        f"- 日预算上限：`${cap:.2f}`",
        f"- 今日已花费：`${spent:.2f}`",
        f"- 今日剩余额度：`${rem:.2f}`（事件预留 `${res:.2f}`，可派单额度 `${max(0.0, rem - res):.2f}`）",
        f"- 本次计划预计花费：`${planned:.2f}`",
        "",
        "## 2. 今日派单（Dispatches）",
        "",
    ]
    if not dispatches:
        lines.append("- 今日无派单任务。")
    else:
        lines.extend(
            [
                "| # | 公司 | 任务类型 | 任务说明 | 预计花费 (USD) | 依据 |",
                "|---:|---|---|---|---:|---|",
            ]
        )
        for idx, d in enumerate(dispatches, start=1):
            t = d.get("ticker") or "-"
            k = d.get("kind") or "research"
            task_str = d.get("task") or d.get("instruction") or ""
            est = float(d.get("est_usd") or 0.0)
            why = d.get("why") or ""
            lines.append(f"| {idx} | **{t}** | `{k}` | {task_str} | ${est:.2f} | {why} |")

    lines.extend(["", "## 3. 暂缓 / 未入选事项（Skipped）", ""])
    if not skipped:
        lines.append("- 无暂缓事项。")
    else:
        for s in skipped:
            lines.append(
                f"- **{s.get('ticker') or '-'}**：{s.get('task') or ''} — 原因：{s.get('reason') or '超出额度/退避中'}"
            )

    lines.extend(["", "## 4. 等待 PM 裁决事项（PM Attention）", ""])
    if not pm_attention:
        lines.append("- 当前无等待 PM 裁决事项。")
    else:
        for p in pm_attention:
            lines.append(f"- **{p.get('ticker') or '-'}**：{p.get('what') or p.get('title') or ''}")

    return "\n".join(lines) + "\n"


def build_director_tools(
    *,
    role: str = "chief_of_staff",
    filesystem_backend: Any = None,
) -> list[BaseTool]:
    """Build tools for the Chief of Staff / Research Director and Dalton automation scheduling."""

    @tool("dalton_coverage_board")
    async def dalton_coverage_board(
        config: RunnableConfig = None,
    ) -> str:
        """Read the cross-company Dalton coverage board, budget snapshot, and ranked task candidates.

        Returns:
            JSON string with `date`, `tz`, `budget`, `companies`, `candidates`, and `est_cost_by_kind`.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        board = await build_coverage_board(user_id=user_id)
        return json.dumps(board, ensure_ascii=False, default=str)

    @tool("dalton_record_plan")
    async def dalton_record_plan(
        dispatches: list[dict[str, Any]],
        skipped: list[dict[str, Any]] | None = None,
        pm_attention: list[dict[str, Any]] | None = None,
        plan_date: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Record the Research Director's daily dispatch plan into Postgres and write `dalton/plans/<date>.md`.

        Refuses the plan if total `est_usd` across `dispatches` exceeds today's allocatable budget
        (`remaining_usd - reserve_for_events_usd`).

        Args:
            dispatches: List of selected tasks (`{ticker, workspace_id, task, kind, est_usd, why}`).
            skipped: List of deferred tasks (`{ticker, task, reason}`).
            pm_attention: List of items awaiting PM review (`{ticker, what}`).
            plan_date: Optional ISO date string (`YYYY-MM-DD`); defaults to today.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        for d in dispatches or []:
            if isinstance(d, dict) and ("workspace_id" in d or d.get("workspace_id")):
                await verify_workspace_access(str(d.get("workspace_id") or ""), user_id)
        board = await build_coverage_board(user_id=user_id)
        budget = board.get("budget") or {}
        cap_usd = float(budget.get("cap_usd") or 0.0)
        allocatable = float(budget.get("allocatable_usd") or 0.0)
        planned_usd = round(
            sum(float(d.get("est_usd") or 0.0) for d in (dispatches or []) if isinstance(d, dict)),
            2,
        )
        if cap_usd > 0 and planned_usd > allocatable + 1e-6:
            return json.dumps(
                {
                    "ok": False,
                    "error": "plan_exceeds_budget",
                    "message": (
                        f"计划总花费 ${planned_usd:.2f} 超出今日可分配额度 ${allocatable:.2f} "
                        f"（剩余 ${float(budget.get('remaining_usd') or 0.0):.2f} − 事件预留 ${float(budget.get('reserve_for_events_usd') or 0.0):.2f}）。请缩减派单列表。"
                    ),
                    "planned_usd": planned_usd,
                    "allocatable_usd": allocatable,
                },
                ensure_ascii=False,
            )

        p_date = plan_date or board.get("date") or date.today().isoformat()
        configurable = (config or {}).get("configurable") or {}
        thread_id = configurable.get("thread_id")

        board_snap = {
            "user_id": user_id,
            "companies_count": len(board.get("companies") or []),
            "candidates": board.get("candidates") or [],
        }

        saved = await plans_db.upsert_daily_plan(
            user_id=user_id,
            plan_date=p_date,
            budget_snapshot=budget,
            board_snapshot=board_snap,
            dispatches=dispatches or [],
            skipped=skipped or [],
            pm_attention=pm_attention if pm_attention is not None else (board.get("pm_handoffs") or []),
            director_thread_id=str(thread_id) if thread_id else None,
        )

        md_text = _render_daily_plan_markdown(
            plan_date=str(p_date),
            budget=budget,
            dispatches=dispatches or [],
            skipped=skipped or [],
            pm_attention=pm_attention if pm_attention is not None else (board.get("pm_handoffs") or []),
        )
        plan_rel_path = f"dalton/plans/{p_date}.md"
        if filesystem_backend is not None:
            try:
                if hasattr(filesystem_backend, "awrite"):
                    await filesystem_backend.awrite(plan_rel_path, md_text)
                elif hasattr(filesystem_backend, "write"):
                    filesystem_backend.write(plan_rel_path, md_text)
            except Exception as e:
                logger.warning("[DALTON_DIRECTOR] write plan markdown failed: %s", e)

        return json.dumps(
            {
                "ok": True,
                "plan_date": str(p_date),
                "planned_usd": planned_usd,
                "allocatable_usd": allocatable,
                "dispatches_count": len(dispatches or []),
                "plan_file": plan_rel_path,
                "plan_markdown": md_text,
                "saved_at": str(saved.get("created_at") or ""),
            },
            ensure_ascii=False,
            default=str,
        )

    @tool("dalton_agenda_note")
    async def dalton_agenda_note(
        workspace_id: str,
        question: str,
        value: str = "high",
        urgency: str = "this_week",
        why: str = "",
        ticker: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Append a PM/Director priority question directly to a company's `agenda` document (`origin='pm'`).

        Does not wake the company's sandbox, saving tokens for simple agenda updates.

        Args:
            workspace_id: Target company's workspace UUID.
            question: The research question or priority focus from PM/Director.
            value: "high" | "medium" | "low".
            urgency: "overdue" | "today" | "this_week" | "this_quarter" | "later".
            why: Context or rationale for adding this question.
            ticker: Optional ticker symbol (e.g. "AMZN").
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        await verify_workspace_access(workspace_id, user_id)
        try:
            from src.dalton.database.knowledge import latest_version
            from src.dalton.knowledge.service import apply_update

            latest = await latest_version(workspace_id, "agenda")
            base_ver = int(latest["version_no"]) if latest else None
            urg_norm = {
                "overdue": "now",
                "today": "now",
                "now": "now",
                "this_week": "this_week",
                "this_quarter": "this_quarter",
                "later": "someday",
                "someday": "someday",
            }.get(str(urgency or "this_week").strip().lower(), "this_week")
            res = await apply_update(
                user_id=user_id,
                workspace_id=workspace_id,
                doc_key="agenda",
                ops=[
                    {
                        "op": "append_item",
                        "path": "/open_questions",
                        "value": {
                            "question": question,
                            "value": value if value in ("high", "medium", "low") else "high",
                            "urgency": urg_norm,
                            "origin": "pm",
                            "owner": "pm",
                            "status": "open",
                            "why": why or "PM 经研究总监追加",
                        },
                    }
                ],
                reason="PM 经研究总监追加",
                base_version=base_ver,
                source="tool",
                backend=None,
                thread_id=str(configurable.get("thread_id")) if configurable.get("thread_id") else None,
            )
            return json.dumps(res, ensure_ascii=False, default=str)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("[DALTON_DIRECTOR] dalton_agenda_note fallback: %s", e)
            return json.dumps(
                {"ok": False, "error": str(e)},
                ensure_ascii=False,
            )

    @tool("dalton_schedule_automation")
    async def dalton_schedule_automation(
        action: str,
        name: str | None = None,
        trigger_type: str = "cron",
        cron_expression: str | None = None,
        next_run_at: str | None = None,
        instruction: str | None = None,
        description: str | None = None,
        timezone_name: str | None = None,
        workspace_id: str | None = None,
        ticker: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Create, update, pause, resume, or list Dalton (`dalton-*`) automations (PK.15).

        Args:
            action: "upsert", "pause", "resume", or "list".
            name: Automation name (must start with `dalton-`, e.g. `dalton-meta-watch`).
            trigger_type: "cron", "once", or "price".
            cron_expression: Standard 5-field cron expression when trigger_type="cron".
            next_run_at: ISO 8601 datetime when trigger_type="once".
            instruction: Prompt instruction executed when the automation fires.
            description: Short description of the automation.
            timezone_name: IANA timezone (defaults to `Asia/Shanghai`).
            workspace_id: Workspace UUID (defaults to current workspace in config).
            ticker: Optional company ticker for naming validation.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        caller_ws_id = str(configurable.get("workspace_id") or "").strip() or None
        ws_id = workspace_id if workspace_id is not None else caller_ws_id
        tz = timezone_name or get_dalton_settings().autonomy.timezone

        spec = {
            "action": action,
            "name": name,
            "trigger_type": trigger_type,
            "cron_expression": cron_expression,
            "next_run_at": next_run_at,
            "instruction": instruction,
            "description": description,
            "timezone": tz,
            "workspace_id": ws_id,
            "ticker": ticker,
        }
        result = await schedule_dalton_automation_helper(
            name,
            spec,
            user_id=user_id,
            workspace_id=ws_id,
            caller_workspace_id=caller_ws_id,
            ticker=ticker,
            role=role,
            action=action,
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    @tool("dalton_coverage_tracking")
    async def dalton_coverage_tracking(
        action: str,
        ticker: str | None = None,
        workspace_id: str | None = None,
        source: str = "user",
        config: RunnableConfig = None,
    ) -> str:
        """Inspect, enable, or disable the standard autonomous tracking suite (`watch`, `delta`, `move-up`, `move-down`) for a company workspace.
        Analysts are restricted to their current workspace and refused on any other company; Chief of Staff can target any company workspace via `ticker` or `workspace_id`.

        Args:
            action: `"status"` to inspect tracking state and next run times, `"enable"` to create or resume the standard tracking suite, or `"disable"` to pause all `dalton-<ticker>-*` tasks (including `-earn-*`) without deleting them.
            ticker: Company ticker symbol (e.g. `"NVDA"`). Used by Chief of Staff when `workspace_id` is omitted; rejected for analysts if it does not match their current workspace.
            workspace_id: Target company workspace UUID. Defaults to the active workspace for analysts.
            source: `"user"` (default, explicit user/conversation enable with `force=True`) or `"coverage_finish"` (post-coverage auto-enable that respects `paused_by_user`).

        Returns:
            JSON object with derived status (`enabled`, `disabled`, or `incomplete`), `paused_by_user` flag, and each task's schedule and `next_run_at`.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")

        act = str(action or "").strip().lower()
        if act not in {"status", "enable", "disable"}:
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": f"不支持的 action `{action}`，仅支持 `status`、`enable`、`disable`。",
                },
                ensure_ascii=False,
            )

        src = str(source or "user").strip().lower()
        if src not in {"user", "coverage_finish"}:
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_source",
                    "message": f"不支持的 source `{source}`，仅支持 `user` 或 `coverage_finish`。",
                },
                ensure_ascii=False,
            )

        current_ws = str(configurable.get("workspace_id") or "").strip()
        passed_ws = str(workspace_id or "").strip()
        passed_ticker = str(ticker or "").strip().upper()

        if role != "chief_of_staff":
            if not current_ws or current_ws.lower() == "home":
                return json.dumps(
                    {
                        "ok": False,
                        "error": "forbidden_workspace",
                        "message": "公司分析师必须在当前公司工作区内调用此工具。",
                    },
                    ensure_ascii=False,
                )
            if passed_ws and passed_ws != current_ws:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "forbidden_workspace",
                        "message": "公司分析师只能操作自己所在的工作区，禁止操作其他公司工作区。",
                    },
                    ensure_ascii=False,
                )
            try:
                cur_ticker = str(await _resolve_workspace_ticker(current_ws) or "").strip().upper()
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "forbidden_workspace",
                        "message": f"无法解析当前工作区（{current_ws}）对应的公司 ticker，拒绝操作：{exc}",
                    },
                    ensure_ascii=False,
                )
            if not cur_ticker:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "forbidden_workspace",
                        "message": f"无法解析当前工作区（{current_ws}）对应的公司 ticker，拒绝操作。",
                    },
                    ensure_ascii=False,
                )
            if passed_ticker:
                if passed_ticker != cur_ticker:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "forbidden_workspace",
                            "message": f"公司分析师只能操作当前工作区（{cur_ticker}），禁止操作 {passed_ticker}。",
                        },
                        ensure_ascii=False,
                    )
                resolved_by_ticker = await resolve_workspace_id_by_ticker(passed_ticker, user_id)
                if resolved_by_ticker is not None and str(resolved_by_ticker).strip() != current_ws:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "forbidden_workspace",
                            "message": f"公司分析师只能操作当前工作区，禁止操作其他公司（{passed_ticker}）。",
                        },
                        ensure_ascii=False,
                    )
            target_ws_id = current_ws
        else:
            if passed_ws:
                await verify_workspace_access(passed_ws, user_id)
                target_ws_id = passed_ws
            elif passed_ticker:
                resolved_ws = await resolve_workspace_id_by_ticker(passed_ticker, user_id)
                if not resolved_ws:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "workspace_not_found",
                            "message": f"未找到 {passed_ticker} 对应的公司工作区。",
                        },
                        ensure_ascii=False,
                    )
                target_ws_id = resolved_ws
            elif current_ws and current_ws.lower() != "home":
                await verify_workspace_access(current_ws, user_id)
                target_ws_id = current_ws
            else:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "missing_target",
                        "message": "Chief of Staff 必须通过 `ticker` 或 `workspace_id` 指定目标公司工作区。",
                    },
                    ensure_ascii=False,
                )

        if act == "status":
            res = await get_tracking_status(user_id, target_ws_id)
        elif act == "enable":
            is_user_action = src == "user"
            res = await enable_tracking(
                user_id,
                target_ws_id,
                force=is_user_action,
                respect_user_pause=not is_user_action,
            )
        else:
            res = await disable_tracking(user_id, target_ws_id)
        return json.dumps(res, ensure_ascii=False, default=str)

    @tool("dalton_system_jobs")
    async def dalton_system_jobs(
        action: str = "list",
        key: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Inspect, toggle, or immediately trigger Dalton system jobs across company, Home, host, and ops scopes.

        Use when the user asks to view all scheduled jobs, enable/disable global Home tasks
        (`dalton-earnings-calendar`, `dalton-director-daily`, `dalton-director-weekly`, `dalton-data-health`),
        toggle or trigger host `conversation-archive`, or manage company auto-tracking (`company:<TICKER>`).
        Ops jobs (`ops-*`) are strictly read-only. When `action="run_now"` succeeds, always inform the user
        that the job runs asynchronously in the background and state where to check the result (`where_to_check`).

        Args:
            action: `"list"` (all jobs), `"status"` (single job by `key`), `"enable"`, `"disable"`, or `"run_now"`.
            key: Job key (e.g. `"dalton-data-health"`, `"conversation-archive"`, `"company:NVDA"`, `"ops-db-backup"`). Required except for `"list"`.

        Returns:
            JSON object with job manifest/status (`on` | `off` | `partial` | `unknown`) or async `run_now` dispatch details (`async`, `where_to_check`, `message`).
        """
        from src.dalton.autonomy.system_jobs import (
            classify_system_job_key,
            get_system_job_status,
            list_system_jobs,
            run_system_job_now,
            toggle_system_job,
        )
        from src.dalton.autonomy.tracking import (
            _resolve_workspace_ticker,
            resolve_workspace_id_by_ticker,
        )

        act = str(action or "").strip().lower()
        if act not in ("list", "status", "enable", "disable", "run_now"):
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_action",
                    "message": "action 必须为 list、status、enable、disable 或 run_now",
                },
                ensure_ascii=False,
            )

        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        current_ws = str(configurable.get("workspace_id") or "").strip() or None
        raw_key = str(key or "").strip() or None

        if role != "chief_of_staff":
            if not current_ws or current_ws.lower() == "home":
                return json.dumps(
                    {
                        "ok": False,
                        "error": "missing_workspace",
                        "message": "公司分析师必须在当前公司工作区内调用此工具。",
                    },
                    ensure_ascii=False,
                )
            if raw_key:
                scope, target = classify_system_job_key(raw_key)
                if scope != "company":
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "forbidden_scope",
                            "message": f"公司分析师只能管理本公司任务，禁止操作 {scope} 级任务（{raw_key}）。",
                        },
                        ensure_ascii=False,
                    )
                try:
                    cur_ticker = str(await _resolve_workspace_ticker(current_ws) or "").strip().upper()
                except Exception as exc:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "forbidden_workspace",
                            "message": f"无法解析当前工作区（{current_ws}）对应的公司 ticker，拒绝操作：{exc}",
                        },
                        ensure_ascii=False,
                    )
                if not cur_ticker:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "forbidden_workspace",
                            "message": f"无法解析当前工作区（{current_ws}）对应的公司 ticker，拒绝操作。",
                        },
                        ensure_ascii=False,
                    )
                norm_target = str(target or "").strip()
                if norm_target != current_ws:
                    if norm_target.upper() != cur_ticker:
                        return json.dumps(
                            {
                                "ok": False,
                                "error": "forbidden_workspace",
                                "message": f"公司分析师只能操作当前工作区（{cur_ticker}），禁止操作 {target}。",
                            },
                            ensure_ascii=False,
                        )
                    resolved_ws = await resolve_workspace_id_by_ticker(norm_target.upper(), user_id)
                    if resolved_ws is not None and str(resolved_ws).strip() != current_ws:
                        return json.dumps(
                            {
                                "ok": False,
                                "error": "forbidden_workspace",
                                "message": f"公司分析师只能管理当前工作区，禁止操作其他公司（{target}）。",
                            },
                            ensure_ascii=False,
                        )

            await verify_workspace_access(current_ws, user_id)
            if act in ("list", "status"):
                res = await get_system_job_status(user_id, current_ws)
                return json.dumps(res, ensure_ascii=False, default=str)
            if act in ("enable", "disable"):
                return await dalton_coverage_tracking.ainvoke(
                    {"action": act, "workspace_id": current_ws},
                    config=config,
                )
            try:
                res = await run_system_job_now(user_id, current_ws)
                return json.dumps(res, ensure_ascii=False, default=str)
            except Exception as exc:
                return json.dumps(
                    {"ok": False, "error": "system_job_error", "message": str(exc)},
                    ensure_ascii=False,
                )

        # Chief of Staff
        if act == "list" or (act == "status" and not raw_key):
            res = await list_system_jobs(user_id)
            return json.dumps(res, ensure_ascii=False, default=str)

        if not raw_key:
            return json.dumps(
                {
                    "ok": False,
                    "error": "missing_key",
                    "message": f"执行 {act} 时必须提供任务 key（如 dalton-data-health、conversation-archive、company:NVDA）。",
                },
                ensure_ascii=False,
            )

        try:
            scope, target = classify_system_job_key(raw_key)
        except ValueError as exc:
            return json.dumps(
                {"ok": False, "error": "invalid_key", "message": str(exc)},
                ensure_ascii=False,
            )

        if scope == "ops" and act in ("enable", "disable", "run_now"):
            return json.dumps(
                {
                    "ok": False,
                    "error": "ops_readonly",
                    "message": "运维任务（ops）为只读展示，禁止在系统内开关或立即运行。",
                },
                ensure_ascii=False,
            )

        try:
            if act == "status":
                res = await get_system_job_status(user_id, raw_key)
            elif act in ("enable", "disable"):
                res = await toggle_system_job(user_id, raw_key, enabled=(act == "enable"))
            else:
                res = await run_system_job_now(user_id, raw_key)
            return json.dumps(res, ensure_ascii=False, default=str)
        except PermissionError as exc:
            err_code = "ops_readonly" if scope == "ops" else "forbidden_pm"
            return json.dumps(
                {"ok": False, "error": err_code, "message": str(exc)},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error": "system_job_error", "message": str(exc)},
                ensure_ascii=False,
            )

    if role == "chief_of_staff":
        return [
            dalton_coverage_board,
            dalton_record_plan,
            dalton_agenda_note,
            dalton_schedule_automation,
            dalton_coverage_tracking,
            dalton_system_jobs,
        ]
    return [dalton_schedule_automation, dalton_coverage_tracking, dalton_system_jobs]
