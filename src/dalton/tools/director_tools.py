# ruff: noqa: BLE001
"""Research Director & automation scheduling tools for Dalton (PK.15, PK.19)."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.auth import (
    resolve_caller_home_workspace_id,
    verify_workspace_access,
)
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
from src.dalton.database import watch as watch_db
from src.dalton.settings import get_dalton_settings, is_coverage_units_enabled

logger = logging.getLogger(__name__)

_QUARTER_PREFIX_RE = re.compile(
    r"^\s*(FY\d{2,4}[\s-]*(?:Q[1-4]|H[12]|FY)|[1-4]Q\d{2,4}|[12]H\d{2,4}|\d{4}[\s-]*(?:Q[1-4]|H[12])|F?Q[1-4][\s-]*\d{2,4}|FY\d{2,4})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _extract_quarter_prefix(note: str | None) -> str | None:
    """Extract normalized leading fiscal quarter token (e.g. ``FY26Q3``) from ``note``."""
    raw = str(note or "").strip()
    if not raw:
        return None
    m = _QUARTER_PREFIX_RE.match(raw)
    if not m:
        return None
    return re.sub(r"[\s-]+", "", m.group(1)).upper()


def _contains_date_token(text: str, token: str) -> bool:
    """Return True iff ``token`` appears in ``text`` as a complete date token (so ``10/1`` does not match ``10/15``)."""
    if not text or not token:
        return False
    pattern = rf"(?<![0-9/\-]){re.escape(token)}(?![0-9/\-])"
    return bool(re.search(pattern, text))


def _format_rescheduled_note(base_note: str | None, old_date: str, new_date: str) -> str:
    """Preserve ``old_date`` in ``note`` when an event is rescheduled (e.g. ``原 10/27，改期``)."""
    clean_old = str(old_date or "").strip()
    clean_new = str(new_date or "").strip()
    note_str = str(base_note or "").strip()
    if not clean_old or not clean_new or clean_old == clean_new:
        return note_str

    mm_dd = clean_old
    mm_dd_padded = clean_old
    parts = clean_old.split("-")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        mm_dd = f"{int(parts[1])}/{int(parts[2])}"
        mm_dd_padded = f"{int(parts[1]):02d}/{int(parts[2]):02d}"

    if any(_contains_date_token(note_str, tok) for tok in (clean_old, mm_dd, mm_dd_padded)):
        if "改期" not in note_str:
            return f"{note_str}（改期）"
        return note_str

    tag = f"原 {mm_dd}，改期" if mm_dd == clean_old else f"原 {mm_dd}，改期（{clean_old}）"
    return f"{note_str}（{tag}）" if note_str else tag


_UNSET_DATE = "__UNSET__"


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


class _InputRefused(ValueError):
    """A cross-unit request's association failed validation (codex r1 #11).

    Distinct from a plain ValueError so the tool layer can return the specific
    error code instead of a generic invalid_input.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
            When `coverage_units.enabled` is on it ALSO carries `units`: every registered coverage
            unit (sector and company) with `readable` / `delegatable` / `auto_tracked` flags, sector
            members and each member's `workspace_created`; `companies` keeps its exact shape either way.
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

    @tool("dalton_cross_unit_request")
    async def dalton_cross_unit_request(
        action: str,
        request_id: str | None = None,
        question: str | None = None,
        sector_workspace_id: str | None = None,
        sector_unit_id: str | None = None,
        target_workspace_id: str | None = None,
        evidence_needed: list[str] | None = None,
        completion_condition: str | None = None,
        status: str | None = None,
        dispatch_thread_id: str | None = None,
        result: list[dict[str, Any]] | None = None,
        limit: int = 20,
        cursor: list[str] | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Track cross-layer research requests between a sector workspace and company workspaces (P2).

        The sector analyst reports a structured evidence request (target unit, question, evidence
        needed, completion condition); you persist it here (`action="create"`), dispatch it to the
        company analyst with `delegate_to_analyst(report_back=True)` yourself, record the dispatch
        (`action="update"`, `status="dispatched"`, `dispatch_thread_id=...`), and when the report
        comes back record the outcome (`status="fulfilled"`, `result=[{...}]` with completion state,
        knowledge doc versions, artifact refs) before handing the result to the sector thread.
        A dispatch that fails, is stopped, or lacks permission becomes `status="gap"` and STAYS
        visible as a deficiency until re-dispatched or cancelled — never silently dropped.
        `action="retry"` re-fires YOUR pending linkage gaps (`kind="linkage_retry"`, auto-recorded
        when a cross-unit linkage could not be written to the target agenda). Retries run from this
        Home thread with backend=None; the target's next turn start applies the pending agenda file,
        so there is no conflict with its own end-of-turn reconcile. On update the MERGED
        target/thread/status is validated, not just the newly passed fields. Dispatching still goes
        through `delegate_to_analyst`; this tool only persists the dependency. Association ownership
        is validated: `sector_unit_id` must be your own or a readable sector unit; `target_workspace_id`
        a workspace you may delegate to; and `dispatch_thread_id` a thread that actually belongs to
        that target workspace.

        Args:
            action: `"create"` | `"list"` | `"update"` | `"retry"`.
            limit: Batch size for `"retry"` (oldest gaps first; the response reports `remaining`).
            cursor: Keyset cursor for `"retry"` paging — pass the previous call's `next_cursor`
                (a `[created_at, request_id]` pair) so rows that stay `skipped_no_access` don't block
                later recoverable ones; omit it to start from the oldest gap.
            request_id: Request UUID (required for `"update"`).
            question: The research question to answer (required for `"create"`).
            sector_workspace_id: Reporting sector workspace UUID.
            sector_unit_id: Reporting sector coverage unit UUID (must be yours or readable).
            target_workspace_id: Company workspace the request is dispatched to (must be delegatable).
            evidence_needed: Evidence items the company should collect (list of strings).
            completion_condition: What counts as done.
            status: `"open"` | `"dispatched"` | `"fulfilled"` | `"gap"` | `"cancelled"`.
            dispatch_thread_id: The delegate_to_analyst thread id (required when marking dispatched, unless already set; must belong to the target workspace).
            result: Report outcome entries: completion state, knowledge doc + version refs, artifact refs.

        Returns:
            JSON object with `ok` and the created/updated/listed request rows.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")

        from src.dalton.database import coverage_units as cu_db
        from src.dalton.database import cross_unit_requests as cur_db

        async def _verify_sector_unit(raw_unit_id: str) -> None:
            """codex r1 #11：sector_unit_id 必须是本人的，或其工作区可读的 sector 单元。"""
            clean = str(raw_unit_id or "").strip()
            if not clean:
                return
            unit = await cu_db.get_coverage_unit(clean)
            if unit is None or str(unit.get("kind") or "") != "sector":
                raise _InputRefused(
                    "invalid_sector_unit",
                    f"sector_unit_id={clean} 不是已登记的 sector 覆盖单元。",
                )
            if str(unit.get("owner_user_id") or "") != user_id:
                bound_ws = str(unit.get("workspace_id") or "").strip()
                if not bound_ws:
                    raise _InputRefused(
                        "invalid_sector_unit",
                        f"sector 单元 {clean} 属于他人且未绑定工作区，无法核实访问权。",
                    )
                await verify_workspace_access(bound_ws, user_id, need="read")

        async def _verify_dispatch_thread(raw_thread_id: str, effective_target: str) -> None:
            """codex r1 #11：dispatch_thread_id 必须是确实属于目标工作区的线程。"""
            from src.server.database.conversation.threads_read import get_thread_by_id

            thread = await get_thread_by_id(str(raw_thread_id).strip())
            if thread is None or str(thread.get("workspace_id") or "") != str(
                effective_target
            ):
                raise _InputRefused(
                    "invalid_dispatch_thread",
                    f"dispatch_thread_id={raw_thread_id} 不存在，或不属于目标工作区 {effective_target}。",
                )

        act = str(action or "").strip().lower()
        try:
            if act == "create":
                if not str(question or "").strip():
                    return json.dumps(
                        {"ok": False, "error": "invalid_input",
                         "message": "action=create 需要非空 question（结构化取证问题）。"},
                        ensure_ascii=False,
                    )
                if sector_workspace_id and str(sector_workspace_id).strip():
                    await verify_workspace_access(
                        str(sector_workspace_id).strip(), user_id, need="read"
                    )
                await _verify_sector_unit(sector_unit_id or "")
                if target_workspace_id and str(target_workspace_id).strip():
                    # 目标必须是调用者有权委派的工作区（codex r1 #11）
                    await verify_workspace_access(
                        str(target_workspace_id).strip(), user_id,
                        need="write", channel="cos",
                    )
                row = await cur_db.insert_request(
                    owner_user_id=user_id,
                    question=str(question or ""),
                    sector_unit_id=sector_unit_id,
                    sector_workspace_id=sector_workspace_id,
                    target_workspace_id=target_workspace_id,
                    evidence_needed=[
                        {"what": str(e)} for e in (evidence_needed or []) if str(e or "").strip()
                    ],
                    completion_condition=completion_condition or "",
                )
                return json.dumps({"ok": True, "request": row}, ensure_ascii=False, default=str)

            if act == "list":
                clean_status = str(status or "").strip() or None
                rows = await cur_db.list_requests(user_id, status=clean_status)
                counts: dict[str, int] = {}
                for r in rows:
                    counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
                return json.dumps(
                    {"ok": True, "requests": rows, "status_counts": counts},
                    ensure_ascii=False,
                    default=str,
                )

            if act == "update":
                rid = str(request_id or "").strip()
                if not rid:
                    return json.dumps(
                        {"ok": False, "error": "missing_request_id",
                         "message": "action=update 需要 request_id。"},
                        ensure_ascii=False,
                    )
                existing = await cur_db.get_request(rid, user_id)
                if existing is None:
                    return json.dumps(
                        {"ok": False, "error": "request_not_found",
                         "message": f"未找到属于当前用户的跨层请求 {rid}。"},
                        ensure_ascii=False,
                    )
                clean_status = str(status or "").strip() or None
                new_target = (
                    str(target_workspace_id).strip()
                    if target_workspace_id and str(target_workspace_id).strip()
                    else ""
                )
                new_thread = (
                    str(dispatch_thread_id).strip()
                    if dispatch_thread_id and str(dispatch_thread_id).strip()
                    else ""
                )
                # codex r2 #5：校验【合并后的有效值】，不是只看本次传入——
                # 保留旧行的 target/thread 时同样要过归属检查。
                eff_target = new_target or str(existing.get("target_workspace_id") or "")
                eff_thread = new_thread or str(existing.get("dispatch_thread_id") or "")
                eff_status = clean_status or str(existing.get("status") or "")
                # 仅当本次更新涉及派单关联（换目标 / 绑线程 / 标 dispatched）时
                # 做全量校验；fulfilled/cancelled/gap 的收尾记账不因历史权限
                # 变化被卡死。
                touches_dispatch = bool(new_target or new_thread or clean_status == "dispatched")
                if touches_dispatch:
                    if eff_target:
                        await verify_workspace_access(
                            eff_target, user_id, need="write", channel="cos"
                        )
                    if eff_thread:
                        if not eff_target:
                            return json.dumps(
                                {"ok": False, "error": "missing_target",
                                 "message": "绑定 dispatch_thread_id 前必须先指定目标工作区（本次传入或请求行已有）。"},
                                ensure_ascii=False,
                            )
                        await _verify_dispatch_thread(eff_thread, eff_target)
                    if eff_status == "dispatched" and not eff_thread:
                        return json.dumps(
                            {"ok": False, "error": "missing_dispatch_thread",
                             "message": "标记 dispatched 时必须同时提供 dispatch_thread_id（delegate_to_analyst 返回的线程 id）。"},
                            ensure_ascii=False,
                        )
                if clean_status == "fulfilled" and not (
                    result or (existing.get("result") or [])
                ):
                    return json.dumps(
                        {"ok": False, "error": "missing_result",
                         "message": "标记 fulfilled 时必须提供 result（完成状态、知识版本与产物引用）。"},
                        ensure_ascii=False,
                    )
                # Tool-level omitted args mean "leave unchanged" — only fields
                # the caller actually passed reach the DB layer.
                update_kwargs: dict[str, Any] = {"status": clean_status}
                if new_target:
                    update_kwargs["target_workspace_id"] = new_target
                if new_thread:
                    update_kwargs["dispatch_thread_id"] = new_thread
                if result:
                    update_kwargs["result"] = [
                        r if isinstance(r, dict) else {"note": str(r)} for r in result
                    ]
                row = await cur_db.update_request(rid, user_id, **update_kwargs)
                return json.dumps({"ok": True, "request": row}, ensure_ascii=False, default=str)

            if act == "retry":
                from src.dalton.knowledge.cross_unit import retry_linkage_rows

                retry_target = (
                    str(target_workspace_id).strip()
                    if target_workspace_id and str(target_workspace_id).strip()
                    else None
                )
                try:
                    batch_limit = max(1, min(int(limit or 20), 100))
                except (TypeError, ValueError):
                    batch_limit = 20
                parsed_cursor: tuple[str, str] | None = None
                if cursor:
                    if not isinstance(cursor, (list, tuple)) or len(cursor) != 2:
                        return json.dumps(
                            {"ok": False, "error": "invalid_cursor",
                             "message": "cursor 必须是上一次返回的 next_cursor（[created_at, request_id] 两个元素）。"},
                            ensure_ascii=False,
                        )
                    parsed_cursor = (str(cursor[0]), str(cursor[1]))
                # 从 Home 线程发起：写入目标 agenda 用 backend=None，目标工作区
                # 下一回合开始由 apply_pending 落盘（先于它自己回合末的
                # detect_direct_edits，不会误判为直接编辑）。
                outcome = await retry_linkage_rows(
                    user_id,
                    target_workspace_id=retry_target,
                    limit=batch_limit,
                    cursor=parsed_cursor,
                )
                next_cursor = outcome.get("next_cursor")
                return json.dumps(
                    {
                        "ok": True,
                        "results": outcome.get("results"),
                        "remaining": outcome.get("remaining"),
                        "next_cursor": list(next_cursor) if next_cursor else None,
                    },
                    ensure_ascii=False,
                    default=str,
                )

            return json.dumps(
                {"ok": False, "error": "invalid_action",
                 "message": f"不支持的 action `{action}`，仅支持 create、list、update、retry。"},
                ensure_ascii=False,
            )
        except HTTPException:
            raise
        except _InputRefused as refused:
            return json.dumps(
                {"ok": False, "error": refused.code, "message": str(refused)},
                ensure_ascii=False,
            )
        except ValueError as e:
            return json.dumps({"ok": False, "error": "invalid_input", "message": str(e)},
                              ensure_ascii=False)
        except Exception as e:
            logger.warning("[DALTON_DIRECTOR] dalton_cross_unit_request fallback: %s", e)
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

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
        await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
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

    @tool("dalton_watch_event")
    async def dalton_watch_event(
        workspace_id: str,
        type: str | None = None,
        date: str | None = _UNSET_DATE,
        date_status: str | None = None,
        note: str | None = None,
        source: str | None = None,
        plan: str | None = None,
        event_id: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Upsert a watch/catalyst event into a company's `agenda` document (`/watch/events`).

        Updates the item matching `event_id` when passed (returning an error if `event_id` does not
        exist); otherwise matches an existing event with the same `type` and leading fiscal quarter
        identifier in `note` (e.g. `FY26Q3`), or appends a new `W<N>` item. On update, any omitted
        fields keep their existing values; on create, `date_status` must be explicitly provided.
        Preserves the previous date in `note` when rescheduling (e.g. `原 10/27，改期`).
        On validation failure, the returned message includes the expected schema structure
        (失败时返回的信息里带有期望结构，照着补齐再提交).
        Creates a minimal `agenda` document with the event when none exists yet (no separate
        `knowledge_update` call needed for the first event).
        Does not wake the company's sandbox.

        Args:
            workspace_id: Target company's workspace UUID.
            type: "earnings" | "filing" | "guidance_update" | "investor_day" | "regulatory" | "product" | "macro" | "other".
            date: Event ISO date (`YYYY-MM-DD`) or null; omit on update to keep existing date.
            date_status: "confirmed" | "estimated" | "unknown" (required when creating a new event).
            note: Event summary starting with fiscal quarter for earnings (e.g. "FY26Q3 财报 · 盘后 16:00 ET · 电话会 16:30").
            source: Official source URL or description.
            plan: Planned automation task names (e.g. "T-3 dalton-googl-earn-fy26q3-tm3 / T0 … / T+1 …").
            event_id: Optional existing event ID (`W1`, `W2`, ...).
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
        try:
            from src.dalton.database import knowledge as db_knowledge
            from src.dalton.knowledge import service as knowledge_service
            from src.dalton.knowledge.ops import next_item_id

            async def _attempt() -> tuple[dict[str, Any], bool]:
                """One read-match-write pass. Returns ``(apply_update result, was_first_write)``.

                On a workspace with no agenda yet the pass is a single ``create``
                whose value carries the event inline — ``apply_ops`` accepts only a
                lone create op against an empty base, and the schema's required
                top-level fields (``schema_version``/``as_of``/…) are filled by
                ``_ensure_nested_ids_and_defaults``, so the minimal create value is
                just ``{"watch": {"events": [event]}}``.
                """
                latest = await db_knowledge.latest_version(workspace_id, "agenda")
                base_ver = int(latest["version_no"]) if latest else None
                content = (latest.get("content") or {}) if isinstance(latest, dict) else {}
                watch_block = content.get("watch") if isinstance(content.get("watch"), dict) else {}
                existing_events = [
                    ev for ev in (watch_block.get("events") or []) if isinstance(ev, dict)
                ]

                clean_event_id = str(event_id or "").strip()
                matched_ev: dict[str, Any] | None = None
                match_type = str(type).strip() if (type is not None and str(type).strip()) else "earnings"
                if clean_event_id:
                    matched_ev = next(
                        (ev for ev in existing_events if str(ev.get("id") or "") == clean_event_id),
                        None,
                    )
                    if matched_ev is None:
                        return {
                            "ok": False,
                            "error": "event_not_found",
                            "event_id": clean_event_id,
                            "message": f"agenda.watch.events 中不存在 event_id={clean_event_id!r} 的事件，无法按该 ID 更新。",
                        }, False
                    target_id = clean_event_id
                else:
                    q_prefix = _extract_quarter_prefix(note)
                    if q_prefix:
                        matched_ev = next(
                            (
                                ev
                                for ev in existing_events
                                if str(ev.get("type") or "") == match_type
                                and _extract_quarter_prefix(ev.get("note")) == q_prefix
                            ),
                            None,
                        )
                    if matched_ev is not None and matched_ev.get("id"):
                        target_id = str(matched_ev["id"])
                    else:
                        target_id = next_item_id("events", existing_events)

                date_status_provided = date_status is not None and str(date_status).strip() != ""
                if matched_ev is None and not date_status_provided:
                    return {
                        "ok": False,
                        "error": "missing_date_status",
                        "message": "新增监控事件时必须显式传入 date_status（'confirmed' | 'estimated' | 'unknown'）。",
                    }, latest is None

                if date == _UNSET_DATE:
                    clean_date = matched_ev.get("date") if matched_ev is not None else None
                else:
                    clean_date = str(date).strip() if (date is not None and str(date).strip()) else None

                eff_type = (
                    str(type).strip()
                    if (type is not None and str(type).strip())
                    else (str(matched_ev.get("type") or "earnings") if matched_ev is not None else "earnings")
                )
                eff_date_status = (
                    str(date_status).strip()
                    if date_status_provided
                    else str(matched_ev.get("date_status") or "confirmed")
                )

                eff_note = note if note is not None else (matched_ev.get("note") if matched_ev else None)
                if matched_ev is not None:
                    old_date = str(matched_ev.get("date") or "").strip()
                    if old_date and clean_date and old_date != clean_date:
                        eff_note = _format_rescheduled_note(eff_note, old_date, clean_date)

                event_obj: dict[str, Any] = {
                    "id": target_id,
                    "type": eff_type,
                    "date": clean_date,
                    "date_status": eff_date_status,
                }
                if eff_note is not None or (matched_ev is not None and "note" in matched_ev):
                    event_obj["note"] = eff_note

                if source is not None and str(source).strip():
                    event_obj["source"] = str(source).strip()
                elif source is None and matched_ev is not None and matched_ev.get("source"):
                    event_obj["source"] = str(matched_ev["source"])

                if plan is not None and str(plan).strip():
                    event_obj["plan"] = str(plan).strip()
                elif (
                    plan is None
                    and matched_ev is not None
                    and matched_ev.get("plan")
                    and event_obj["date_status"] != "estimated"
                ):
                    event_obj["plan"] = str(matched_ev["plan"])

                first_write = latest is None
                if first_write:
                    ops: list[dict[str, Any]] = [
                        {"op": "create", "value": {"watch": {"events": [event_obj]}}}
                    ]
                else:
                    ops = [
                        {
                            "op": "upsert_item",
                            "path": "/watch/events",
                            "value": event_obj,
                        }
                    ]
                    if matched_ev is not None and "plan" in matched_ev and "plan" not in event_obj:
                        ops.append({"op": "remove", "path": f"/watch/events/{target_id}/plan"})

                reason_str = (
                    f"更新议程监控事件 {target_id}"
                    if matched_ev is not None
                    else f"新增议程监控事件 {target_id}" + ("（首建 agenda）" if first_write else "")
                )
                res = await knowledge_service.apply_update(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    doc_key="agenda",
                    ops=ops,
                    reason=reason_str,
                    base_version=base_ver,
                    source="tool",
                    backend=None,
                    thread_id=str(configurable.get("thread_id")) if configurable.get("thread_id") else None,
                )
                if isinstance(res, dict):
                    if res.get("ok"):
                        res["event_id"] = target_id
                        res["action"] = "updated" if matched_ev is not None else "created"
                        res["event"] = event_obj
                    elif res.get("error") not in ("event_not_found", "missing_date_status"):
                        problems = res.get("problems") or [{"path": "/watch/events/0"}]
                        expected = knowledge_service._format_expected_structures("agenda", problems)
                        if expected:
                            res.setdefault("expected_structure", expected)
                            msg = str(res.get("message") or "")
                            if "Expected structure" not in msg:
                                res["message"] = f"{msg}\n\n{expected}".strip()
                return res, first_write

            res, first_write = await _attempt()
            # 并发首写：两个调用同时建 agenda 时，输掉的一方在这里重读最新版本，
            # 按普通 upsert 重试一次；再失败就把冲突原样交回。
            if (
                first_write
                and isinstance(res, dict)
                and res.get("error") == "version_conflict"
            ):
                res, _ = await _attempt()
            return json.dumps(res, ensure_ascii=False, default=str)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("[DALTON_DIRECTOR] dalton_watch_event fallback: %s", e)
            return json.dumps(
                {"ok": False, "error": str(e)},
                ensure_ascii=False,
            )

    @tool("dalton_resolve_signals")
    async def dalton_resolve_signals(
        signal_keys: list[str] | str | None = None,
        probe: str | None = None,
        note: str = "",
        signal_id: str | list[str] | None = None,
        config: RunnableConfig = None,
    ) -> str:
        """Resolve wake signals in the caller's own Home workspace (`dalton_signals`).

        Only resolves signals belonging to the caller's own Home workspace (`resolved_by='cos:<thread_id>'`).
        When resolving by `signal_keys`, `probe` (e.g. `"calendar"`, `"director"`) is required so only
        signals matching both `probe` and `signal_key` are closed; `probe` may be omitted when resolving
        solely by `signal_id` (UUID). Merges `note` as `resolution_note` into the signal's payload.

        Args:
            signal_keys: List of signal keys (e.g. `["weekly:2026-W43"]`) or signal UUIDs to close.
            probe: Signal probe source (e.g. `"calendar"`, `"director"`). Required when resolving by `signal_keys`.
            note: One-sentence summary of how the wake signal was handled (saved to `payload.resolution_note`).
            signal_id: Optional single signal UUID or list of signal UUIDs.
        """
        configurable = (config or {}).get("configurable") or {}
        user_id = str(configurable.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")

        refs: list[str] = []
        for raw in (signal_keys, signal_id):
            if isinstance(raw, str):
                if raw.strip() and raw.strip() not in refs:
                    refs.append(raw.strip())
            elif isinstance(raw, (list, tuple, set)):
                for item in raw:
                    s = str(item or "").strip()
                    if s and s not in refs:
                        refs.append(s)

        clean_note = str(note or "").strip()
        clean_probe = str(probe or "").strip() or None
        if not refs:
            return json.dumps(
                {
                    "ok": False,
                    "resolved_count": 0,
                    "error": "empty_signal_keys",
                    "message": "未关闭任何信号（resolved_count=0）：请提供非空的 signal_keys 或 signal_id。",
                },
                ensure_ascii=False,
            )

        has_key_refs = any(watch_db._try_uuid(r) is None for r in refs)
        if has_key_refs and not clean_probe:
            return json.dumps(
                {
                    "ok": False,
                    "resolved_count": 0,
                    "error": "missing_probe",
                    "message": "按 signal_keys 关闭信号时必须传入 probe（例如 'calendar' 或 'director'）；仅按 signal_id (UUID) 关闭时可省略 probe。",
                },
                ensure_ascii=False,
            )

        home_ws_id = await resolve_caller_home_workspace_id(user_id)
        thread_id = str(configurable.get("thread_id") or "").strip()
        resolved_by = f"cos:{thread_id}" if thread_id else f"cos:{user_id}"

        try:
            resolved_count = int(
                await watch_db.resolve_signals(
                    refs,
                    workspace_id=home_ws_id,
                    probe=clean_probe,
                    resolved_by=resolved_by,
                    resolution_note=clean_note or None,
                )
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("[DALTON_DIRECTOR] dalton_resolve_signals fallback: %s", e)
            return json.dumps(
                {"ok": False, "resolved_count": 0, "error": str(e)},
                ensure_ascii=False,
            )

        if resolved_count <= 0:
            return json.dumps(
                {
                    "ok": False,
                    "resolved_count": 0,
                    "workspace_id": home_ws_id,
                    "probe": clean_probe,
                    "resolved_by": resolved_by,
                    "signal_keys": refs,
                    "note": clean_note,
                    "message": (
                        f"未关闭任何信号（resolved_count=0）：probe={clean_probe!r}, signal_keys={refs} 在当前用户的 Home 工作区中未匹配到未关闭信号，"
                        "请检查 probe 或 signal_key 是否拼写错误、信号是否已关闭，或该信号是否属于其他工作区。"
                    ),
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "ok": True,
                "resolved_count": resolved_count,
                "workspace_id": home_ws_id,
                "probe": clean_probe,
                "resolved_by": resolved_by,
                "signal_keys": refs,
                "note": clean_note,
                "message": f"已关闭 {resolved_count} 条 Home 工作区信号（probe={clean_probe}, resolved_by={resolved_by}）：{clean_note}",
            },
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
        # 跨层请求台账只在覆盖单元开关开启时挂载：开关关闭时 CoS 的工具面
        # 与 upstream 完全一致，公司路径不多一个入口。
        cos_tools = [
            dalton_coverage_board,
            dalton_record_plan,
            dalton_agenda_note,
            dalton_watch_event,
            dalton_resolve_signals,
            dalton_schedule_automation,
            dalton_coverage_tracking,
            dalton_system_jobs,
        ]
        if is_coverage_units_enabled():
            cos_tools.insert(2, dalton_cross_unit_request)
        return cos_tools
    return [dalton_schedule_automation, dalton_coverage_tracking, dalton_system_jobs]
