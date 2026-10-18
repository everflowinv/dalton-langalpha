"""Research Director & automation scheduling tools for Dalton (PK.15, PK.19)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.autonomy.board import build_coverage_board
from src.dalton.autonomy.schedule import validate_automation_name
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
    async def dalton_coverage_board() -> str:
        """Read the cross-company Dalton coverage board, budget snapshot, and ranked task candidates.

        Returns:
            JSON string with `date`, `tz`, `budget`, `companies`, `candidates`, and `est_cost_by_kind`.
        """
        board = await build_coverage_board()
        return json.dumps(board, ensure_ascii=False, default=str)

    @tool("dalton_record_plan")
    async def dalton_record_plan(
        dispatches: list[dict[str, Any]],
        skipped: Optional[list[dict[str, Any]]] = None,
        pm_attention: Optional[list[dict[str, Any]]] = None,
        plan_date: Optional[str] = None,
        config: Optional[RunnableConfig] = None,
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
        board = await build_coverage_board()
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

        existing = await plans_db.get_daily_plan(p_date)
        board_snap = {
            "companies_count": len(board.get("companies") or []),
            "candidates": board.get("candidates") or [],
        }
        if existing:
            revs = list((existing.get("board_snapshot") or {}).get("revisions") or [])
            revs.append(
                {
                    "replaced_at": datetime.now(timezone.utc).isoformat(),
                    "dispatches": existing.get("dispatches") or [],
                }
            )
            board_snap["revisions"] = revs

        saved = await plans_db.upsert_daily_plan(
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
        ticker: Optional[str] = None,
        config: Optional[RunnableConfig] = None,
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
        try:
            from src.dalton.database.knowledge import get_latest_version
            from src.dalton.knowledge.service import apply_update

            configurable = (config or {}).get("configurable") or {}
            user_id = str(configurable.get("user_id") or "local_dev_user")
            latest = await get_latest_version(workspace_id, "agenda", valid_only=False)
            base_ver = int(latest["version_no"]) if latest else None
            res = await apply_update(
                user_id=user_id,
                workspace_id=workspace_id,
                doc_key="agenda",
                ops=[
                    {
                        "op": "append_item",
                        "path": "/questions",
                        "value": {
                            "question": question,
                            "value": value if value in ("high", "medium", "low") else "high",
                            "urgency": urgency,
                            "origin": "pm",
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
        except Exception as e:
            logger.warning("[DALTON_DIRECTOR] dalton_agenda_note fallback: %s", e)
            return json.dumps(
                {"ok": False, "error": str(e)},
                ensure_ascii=False,
            )

    @tool("dalton_schedule_automation")
    async def dalton_schedule_automation(
        action: str,
        name: Optional[str] = None,
        trigger_type: str = "cron",
        cron_expression: Optional[str] = None,
        next_run_at: Optional[str] = None,
        instruction: Optional[str] = None,
        description: Optional[str] = None,
        timezone_name: Optional[str] = None,
        workspace_id: Optional[str] = None,
        ticker: Optional[str] = None,
        config: Optional[RunnableConfig] = None,
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
        from src.server.database import automation as auto_db

        configurable = (config or {}).get("configurable") or {}
        user_id = configurable.get("user_id") or "local_dev_user"
        ws_id = workspace_id or configurable.get("workspace_id")
        tz = timezone_name or get_dalton_settings().autonomy.timezone

        if action == "list":
            rows, total = await auto_db.list_automations(
                user_id=user_id,
                workspace_id=ws_id,
                limit=100,
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
            ]
            return json.dumps(
                {"ok": True, "count": len(dalton_rows), "automations": dalton_rows},
                ensure_ascii=False,
            )

        if not name:
            return json.dumps({"ok": False, "error": "缺少自动任务名称 `name`。"}, ensure_ascii=False)

        valid, err_msg = validate_automation_name(name, ticker=ticker if role != "chief_of_staff" else None)
        if not valid:
            return json.dumps({"ok": False, "error": err_msg}, ensure_ascii=False)

        rows, _ = await auto_db.list_automations(user_id=user_id, workspace_id=ws_id, limit=200)
        existing = next((r for r in rows if r.get("name") == name), None)

        if action in ("pause", "resume"):
            if not existing:
                return json.dumps({"ok": False, "error": f"未找到任务 `{name}`"}, ensure_ascii=False)
            new_status = "paused" if action == "pause" else "active"
            updated = await auto_db.update_automation(
                str(existing["automation_id"]),
                user_id,
                status=new_status,
            )
            return json.dumps(
                {
                    "ok": True,
                    "automation_id": str((updated or existing).get("automation_id")),
                    "name": name,
                    "status": new_status,
                },
                ensure_ascii=False,
            )

        if action == "upsert":
            if not instruction and not existing:
                return json.dumps({"ok": False, "error": "新建任务必须提供 `instruction`。"}, ensure_ascii=False)
            next_dt = None
            if next_run_at:
                next_dt = datetime.fromisoformat(next_run_at.replace("Z", "+00:00"))
            if existing:
                patch: dict[str, Any] = {}
                if instruction is not None:
                    patch["instruction"] = instruction
                if description is not None:
                    patch["description"] = description
                if cron_expression is not None:
                    patch["cron_expression"] = cron_expression
                if next_dt is not None:
                    patch["next_run_at"] = next_dt
                updated = await auto_db.update_automation(
                    str(existing["automation_id"]),
                    user_id,
                    **patch,
                )
                return json.dumps(
                    {
                        "ok": True,
                        "action": "updated",
                        "automation_id": str((updated or existing).get("automation_id")),
                        "name": name,
                    },
                    ensure_ascii=False,
                )
            created = await auto_db.create_automation(
                user_id=user_id,
                name=name,
                description=description,
                trigger_type=trigger_type,
                cron_expression=cron_expression,
                timezone=tz,
                trigger_config=None,
                next_run_at=next_dt,
                instruction=instruction or "",
                workspace_id=ws_id,
                llm_model=None,
                additional_context=None,
                thread_strategy="new",
                conversation_thread_id=None,
                max_failures=10,
                delivery_config=None,
                metadata={"dalton_managed": True, "ticker": ticker.upper() if ticker else None},
            )
            return json.dumps(
                {
                    "ok": True,
                    "action": "created",
                    "automation_id": str(created.get("automation_id")),
                    "name": name,
                },
                ensure_ascii=False,
            )

        return json.dumps({"ok": False, "error": f"不支持的 action: {action}"}, ensure_ascii=False)

    if role == "chief_of_staff":
        return [
            dalton_coverage_board,
            dalton_record_plan,
            dalton_agenda_note,
            dalton_schedule_automation,
        ]
    return [dalton_schedule_automation]
