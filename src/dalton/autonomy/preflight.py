"""Zero-token preflight engine for Dalton autonomous tasks (PK.16).

Runs before `transition_execution(..., to="running")` and before sandbox wakeup
in `AutomationExecutor.execute()`. When no actionable signal is found, the
execution settles immediately as `Outcome.PRECHECK_SKIPPED` (`status='skipped'`,
`skip_reason='precheck'`) with zero LLM tokens and zero sandbox startup.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.dalton.auth import verify_workspace_access
from src.dalton.autonomy.backoff import (
    clear_probe_backoff,
    is_in_backoff,
    record_probe_backoff,
)
from src.dalton.autonomy.probes import ProbeSignal
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.calendar import probe_calendar
from src.dalton.autonomy.probes.cnhk import probe_cnhk_filings
from src.dalton.autonomy.probes.director import probe_director
from src.dalton.autonomy.probes.earnings import probe_earnings
from src.dalton.autonomy.probes.sec import probe_sec
from src.dalton.company_identity import is_company_system_user
from src.dalton.coverage import (
    refresh_due_coverage_groups_for_preflight,
    render_company_coverage_yaml,
    render_coverage_yaml,
    sync_user_coverage_to_home,
)
from src.dalton.database import watch as watch_db
from src.dalton.llm.assist import assist_json
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)

DIRECT_WAKE_SEC_FORMS = frozenset({"10-K", "10-Q", "20-F"})

TRIAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "wake": {"type": "boolean"},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
        "reason": {"type": "string"},
        "relevant_pillar_or_question_id": {"type": ["string", "null"]},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal_key": {"type": "string"},
                    "wake": {"type": "boolean"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "reason": {"type": "string"},
                    "relevant_pillar_or_question_id": {"type": ["string", "null"]},
                },
                "required": ["signal_key", "wake"],
            },
        },
    },
    "required": ["wake", "priority", "reason"],
}


def is_direct_wake_signal(sig: dict[str, Any] | ProbeSignal) -> bool:
    """Return True if a signal should wake directly without LLM triage (G4.3).

    Direct wake rules:
    - SEC 10-K, 10-Q, 20-F -> direct wake
    - SEC 8-K containing Item 2.02 -> direct wake
    - CN/HK (cnhk) P1 filings -> direct wake; P2 filings -> LLM triage
    - Agenda due items (catalyst / validation / open_question / pm_question / next_action) -> direct wake
    - Earnings window (T-3 / T0 / T+1 / release) -> direct wake
    - Director / calendar / schedule / price triggers -> direct wake
    All other SEC forms (8-K without 2.02, Form 4, 13D/G, 6-K, DEF 14A) and cnhk P2 filings go to LLM triage.
    """
    if isinstance(sig, ProbeSignal):
        probe = sig.probe
        priority = sig.priority
        payload = sig.payload or {}
        summary = sig.summary or ""
    else:
        probe = str(sig.get("probe") or "")
        priority = str(sig.get("priority") or "")
        payload = sig.get("payload") if isinstance(sig.get("payload"), dict) else {}
        summary = str(sig.get("summary") or "")

    if probe in ("agenda", "earnings", "calendar", "director", "schedule", "price"):
        # The stale-todo sweep-up (prompt review batch 1 round 3 #11) is a P2
        # cleanup hint, not something to wake a sandbox over on its own: it
        # rides along with whatever else woke this run.
        if payload.get("kind") == "stale_todos":
            return False
        return True

    if probe == "sec":
        form = str(payload.get("form") or "").strip().upper()
        if form in DIRECT_WAKE_SEC_FORMS:
            return True
        if form == "8-K":
            items = payload.get("items") or []
            if isinstance(items, str):
                items = [x.strip() for x in items.split(",") if x.strip()]
            if any("2.02" in str(it) for it in items) or "2.02" in summary:
                return True
        return False

    if probe == "cnhk":
        prio = str(priority or payload.get("priority") or "").strip().upper()
        return prio == "P1"

    return True


async def _triage_signals_with_llm(
    *,
    ticker: Optional[str],
    workspace_id: Optional[str],
    user_id: Optional[str] = None,
    signals: list[dict[str, Any]],
    agenda_doc: Optional[dict[str, Any]] = None,
    watch_state: Optional[dict[str, Any]] = None,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Run single batch ``assist_json(task='preflight.triage')`` on non-direct signals (G4.3 / A2).

    Returns ``(kept_signals, skipped_signals, triage_reason)``.
    Conservative fallback: if ``assist_json`` fails, errors, or returns invalid data, all signals wake.
    """
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    if not signals:
        return [], [], "no_triage_signals"

    thesis_summary = str((watch_state or {}).get("thesis_summary") or "")
    pillars_brief: list[str] = []
    open_questions_brief: list[str] = []

    if persist and workspace_id and not thesis_summary:
        try:
            from src.dalton.database.knowledge import latest_valid_version

            t_row = await latest_valid_version(str(workspace_id), "thesis")
            t_content = (t_row or {}).get("content") if isinstance(t_row, dict) else None
            if isinstance(t_content, dict):
                thesis_summary = str(t_content.get("one_liner") or t_content.get("summary") or "")
                for p in t_content.get("pillars") or []:
                    if isinstance(p, dict):
                        pillars_brief.append(f"{p.get('id', 'P')}: {p.get('name') or p.get('claim') or ''}")
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] latest_valid_version(thesis) failed: %s", e)

    if isinstance(agenda_doc, dict):
        for q in agenda_doc.get("open_questions") or []:
            if isinstance(q, dict) and q.get("status") in (None, "open", "investigating"):
                open_questions_brief.append(f"{q.get('id', 'Q')}: {q.get('question') or q.get('title') or ''}")

    candidates_payload = []
    for s in signals:
        pl = s.get("payload") if isinstance(s.get("payload"), dict) else {}
        candidates_payload.append(
            {
                "signal_key": s.get("signal_key"),
                "probe": s.get("probe"),
                "priority": s.get("priority"),
                "summary": s.get("summary"),
                "form": pl.get("form") or pl.get("doc_type"),
                "items": pl.get("items") or [],
                "title": pl.get("title") or pl.get("description") or pl.get("primary_document") or "",
                "snippet": pl.get("snippet") or "",
            }
        )

    system_prompt = (
        "你是 Dalton 投研系统的零噪音预检分诊器（preflight.triage）。"
        "请判断给定的 SEC 披露（非 2.02 的 8-K、Form 4、13D/G、6-K、DEF 14A）或港股/A 股公告是否具有实质投研价值、"
        "值得唤醒分析师沙箱开展深度取证。例行无实质内容的小额高管减持或纯行政公告应返回 wake=false；"
        "涉及业绩指引、重大合同、并购、核心高管变动、大股东增减持或与核心论点/待查问题相关的信号应返回 wake=true。"
    )
    user_payload = {
        "ticker": ticker or "UNKNOWN",
        "thesis_summary": thesis_summary,
        "pillars": pillars_brief[:8],
        "open_questions": open_questions_brief[:10],
        "signals": candidates_payload,
    }

    try:
        res = await assist_json(
            task="preflight.triage",
            instructions=system_prompt,
            payload=user_payload,
            schema=TRIAGE_OUTPUT_SCHEMA,
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] assist_json(preflight.triage) raised %s; conservative wake fallback", e)
        return list(signals), [], "triage_fallback_wake"

    # Extract structured dict from either AssistResult(.ok, .data) or plain dict mock
    data: Optional[dict[str, Any]] = None
    if isinstance(res, dict):
        if res.get("ok") is False:
            logger.warning("[DALTON_PREFLIGHT] assist_json returned ok=False; conservative wake fallback")
            return list(signals), [], "triage_fallback_wake"
        data = res.get("data") if isinstance(res.get("data"), dict) else res
    else:
        ok_attr = getattr(res, "ok", True)
        if not ok_attr:
            logger.warning("[DALTON_PREFLIGHT] AssistResult.ok=False; conservative wake fallback")
            return list(signals), [], "triage_fallback_wake"
        raw_data = getattr(res, "data", None)
        if isinstance(raw_data, dict):
            data = raw_data

    if not isinstance(data, dict) or "wake" not in data:
        logger.warning("[DALTON_PREFLIGHT] Invalid triage response %r; conservative wake fallback", data)
        return list(signals), [], "triage_fallback_wake"

    batch_wake = bool(data.get("wake", True))
    batch_prio = str(data.get("priority") or "P1").upper()
    if batch_prio not in ("P0", "P1", "P2"):
        batch_prio = "P1"
    batch_reason = str(data.get("reason") or ("triage_wake" if batch_wake else "triage_skip"))
    batch_rel = data.get("relevant_pillar_or_question_id")

    per_key: dict[str, dict[str, Any]] = {}
    if isinstance(data.get("decisions"), list):
        for dec in data["decisions"]:
            if isinstance(dec, dict) and dec.get("signal_key"):
                per_key[str(dec["signal_key"])] = dec

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for sig in signals:
        s_copy = dict(sig)
        s_key = str(s_copy.get("signal_key") or "")
        dec = per_key.get(s_key)
        sig_wake = bool(dec.get("wake")) if dec is not None and "wake" in dec else batch_wake
        sig_prio = str((dec or {}).get("priority") or batch_prio).upper()
        if sig_prio in ("P0", "P1", "P2"):
            s_copy["priority"] = sig_prio
        sig_reason = str((dec or {}).get("reason") or batch_reason)
        sig_rel = (dec or {}).get("relevant_pillar_or_question_id", batch_rel)
        pl = dict(s_copy.get("payload") or {})
        pl["triage"] = {
            "wake": sig_wake,
            "priority": s_copy.get("priority", "P1"),
            "reason": sig_reason,
            "relevant_pillar_or_question_id": sig_rel,
        }
        s_copy["payload"] = pl
        if sig_wake:
            kept.append(s_copy)
        else:
            skipped.append(s_copy)

    return kept, skipped, batch_reason


@dataclass
class PreflightResult:
    """Decision returned by `preflight_check` and `decide`."""

    should_wake: bool
    decision: str  # "wake" | "skip" | "passthrough"
    reason: Optional[str] = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    wake_context: Optional[str] = None
    # Probe names still unfinished when a company-watch preflight times out.
    incomplete_probes: list[str] = field(default_factory=list)

    @property
    def skip(self) -> bool:
        return not self.should_wake

    @property
    def summary(self) -> str:
        if self.signals:
            return "; ".join(str(s.get("summary") or "") for s in self.signals[:3])
        return str(self.reason or self.decision)

    @property
    def context(self) -> Optional[str]:
        return self.wake_context


def _sanitize_untrusted_wake_field(val: Any, max_len: int = 200) -> str:
    """Sanitize untrusted external text for `<dalton_wake>` by stripping CR/LF, removing `<`/`>`, and truncating."""
    cleaned = str(val or "").replace("\r", "").replace("\n", "").replace("<", "").replace(">", "").strip()
    return cleaned[:max_len]


def format_wake_directive(
    *,
    automation_name: str,
    ticker: Optional[str],
    signals: list[dict[str, Any]],
    checked_at: Optional[datetime] = None,
) -> str:
    """Format the `<dalton_wake>` XML directive injected into `additional_context`."""
    ts = (checked_at or datetime.now(timezone.utc)).isoformat()
    ticker_attr = f' ticker="{ticker}"' if ticker else ""
    signal_keys = [str(s.get("signal_key") or "") for s in signals if s.get("signal_key")]
    keys_hint = ", ".join(signal_keys)
    lines = [
        f'<dalton_wake automation="{automation_name}"{ticker_attr} checked_at="{ts}" signal_count="{len(signals)}">',
        "以下是由预检探针检测到的真实变更信号，请优先围绕这些信号开展取证与分析，并在结束前调用 `knowledge_append_verdict` 记录五词结论（在 verdict.trigger_signal 或 verdict.trigger_signals 中传入对应 signal_key 以闭环信号状态）：",
        # R4/21: this turn has no user message; without this line the English
        # tool returns and subagent notices pull the final reply into English
        # on a Chinese thread (thread 6288d100).
        "本回合由自动化唤醒、没有新的用户消息：面向用户的最终回复用本线程用户平时使用的语言撰写（本线程为中文则用中文），不因本指令、工具返回或子任务回报是英文而切换。",
    ]
    for idx, sig in enumerate(signals, start=1):
        probe = sig.get("probe") or "probe"
        prio = sig.get("priority") or "P1"
        key = sig.get("signal_key") or ""
        sig_id = sig.get("signal_id") or ""
        summary = _sanitize_untrusted_wake_field(sig.get("summary") or "")
        id_attr = f", signal_id={sig_id}" if sig_id else ""
        payload = sig.get("payload") if isinstance(sig.get("payload"), dict) else {}
        lines.append(f"  {idx}. [{prio}][{probe}] {summary} (key={key}{id_attr})")
        if probe == "cnhk" or payload.get("source") == "cninfo":
            src_val = _sanitize_untrusted_wake_field(payload.get("source") or "cninfo")
            mkt_val = _sanitize_untrusted_wake_field(payload.get("market") or "")
            date_val = _sanitize_untrusted_wake_field(payload.get("date") or "")
            title_val = _sanitize_untrusted_wake_field(payload.get("title") or "")
            link_val = _sanitize_untrusted_wake_field(payload.get("pdf_link") or "")
            mkt_part = f" | market={mkt_val}" if mkt_val else ""
            lines.append(
                f"     data (untrusted): source={src_val}{mkt_part} | date={date_val} | title={title_val} | pdf_link={link_val}"
            )
    if keys_hint:
        lines.append(f"  trigger_signals: [{keys_hint}]")
    lines.append("</dalton_wake>")
    return "\n".join(lines)


def _extract_ticker(automation: dict[str, Any]) -> Optional[str]:
    meta = automation.get("metadata") or {}
    if isinstance(meta, dict):
        # 行业主体永远没有 ticker，也不允许从名称第二段猜出一个（P3：
        # "dalton-sector-…" 的第二段是 "sector"，猜出来的 ticker 是伪身份）。
        if str(meta.get("subject_kind") or "").strip().lower() == "sector":
            return None
        if meta.get("ticker"):
            return str(meta["ticker"]).upper()
    name = str(automation.get("name") or "").strip().lower()
    if name.startswith("dalton-sector-"):
        return None
    if name.startswith("dalton-"):
        parts = name.split("-")
        if len(parts) >= 3 and parts[1] not in ("earnings", "director", "data", "sector"):
            return parts[1].upper()
    return None


def _determine_kind(automation: dict[str, Any]) -> str:
    meta = automation.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("dalton_kind"):
        return str(meta["dalton_kind"])
    name = str(automation.get("name") or "").strip().lower()
    # 行业名先于通用后缀：`dalton-sector-<slug>-delta` 若落进通用 "-delta"
    # 分支会被当公司 weekly_delta 处理（P3）。
    if name.startswith("dalton-sector-") and name.endswith("-agenda"):
        return "sector_watch"
    if name.startswith("dalton-sector-") and name.endswith("-delta"):
        return "sector_delta"
    if name == "dalton-earnings-calendar":
        return "calendar"
    if name == "dalton-director-daily":
        return "director_daily"
    if name == "dalton-director-weekly":
        return "director_weekly"
    if name == "dalton-data-health":
        return "data_health"
    if name.endswith("-watch"):
        return "company_watch"
    if name.endswith("-delta"):
        return "weekly_delta"
    if "-earn-" in name:
        return "earnings_window"
    if "-move-" in name:
        return "price_move"
    return "custom_dalton"


def is_dalton_automation(automation: dict[str, Any]) -> bool:
    """Return True if this automation is managed by Dalton autonomy."""
    name = str(automation.get("name") or "").strip().lower()
    meta = automation.get("metadata") or {}
    return name.startswith("dalton-") or (isinstance(meta, dict) and bool(meta.get("dalton_managed")))


async def _check_budget_available(user_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Return (False, 'budget_exhausted') if the shared daily USD budget is exhausted (A1, A14)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    try:
        from src.dalton.budget.service import get_budget_today

        snap = await get_budget_today(user_id=user_id)
        cap = float(
            snap["cap_usd"]
            if "cap_usd" in snap and snap["cap_usd"] is not None
            else (snap.get("daily_cap_usd") or 0.0)
        )
        spent = float(
            snap["spent_usd"]
            if "spent_usd" in snap and snap["spent_usd"] is not None
            else (snap.get("spent_today_usd") or 0.0)
        )
        reserved = float(
            snap["open_reserved_usd"]
            if "open_reserved_usd" in snap and snap["open_reserved_usd"] is not None
            else (snap.get("reserved_usd") or 0.0)
        )
        if cap > 0 and (spent + reserved) >= cap:
            return False, "budget_exhausted"
    except Exception as e:
        # P0I §5 #7: Preflight fails open here (only avoids skipping wake on transient DB error);
        # actual cash admission in dalton_complete / enforce_local_admission is strictly fail-closed.
        logger.error("[DALTON_PREFLIGHT] budget check failed (failing open at preflight): %s", e)
    return True, None


async def _defer_once_automation_on_budget_exhausted(
    automation: dict[str, Any],
    *,
    persist: bool,
) -> None:
    """Postpone a one-time (`once`) automation by +1 day when skipped due to budget exhaustion (A9)."""
    if str(automation.get("trigger_type") or "") != "once":
        return
    base_dt: datetime
    raw_next = automation.get("next_run_at") or automation.get("last_run_at")
    if isinstance(raw_next, datetime):
        base_dt = raw_next if raw_next.tzinfo is not None else raw_next.replace(tzinfo=timezone.utc)
    elif isinstance(raw_next, str) and raw_next.strip():
        try:
            parsed = datetime.fromisoformat(raw_next.strip().replace("Z", "+00:00"))
            base_dt = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            base_dt = datetime.now(timezone.utc)
    else:
        base_dt = datetime.now(timezone.utc)

    deferred_dt = base_dt + timedelta(days=1)
    automation["next_run_at"] = deferred_dt
    automation["status"] = "active"

    automation_id = str(automation.get("automation_id") or "") or None
    user_id = str(automation.get("user_id") or "").strip() or None
    if persist and automation_id and user_id:
        try:
            from src.server.database.pool import get_db_connection

            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE automations
                        SET status = 'active',
                            next_run_at = COALESCE(next_run_at, last_run_at, NOW()) + INTERVAL '1 day'
                        WHERE automation_id = %s
                          AND user_id = %s
                          AND status IN ('active', 'executing')
                        """,
                        (automation_id, user_id),
                    )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] defer once automation failed for %s: %s", automation_id, e)


@dataclass(frozen=True)
class _VerifiedAutomationOwnership:
    """Verified `(user_id, workspace_id)` ownership result required by `_preflight_check_inner`."""

    user_id: str
    workspace_id: str


async def _verify_automation_workspace_ownership(
    automation: dict[str, Any],
) -> _VerifiedAutomationOwnership:
    """Verify ``user_id`` and ``workspace_id`` ownership for a Dalton automation fail-closed."""
    raw_user_id = automation.get("user_id")
    if not raw_user_id or not str(raw_user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(raw_user_id).strip()
    raw_ws_id = automation.get("workspace_id")
    workspace_id = str(raw_ws_id).strip() if raw_ws_id is not None else ""
    if not workspace_id or workspace_id.lower() == "home":
        raise HTTPException(
            status_code=403,
            detail="Valid workspace_id is required for Dalton automation execution",
        )
    await verify_workspace_access(workspace_id, user_id)
    return _VerifiedAutomationOwnership(user_id=user_id, workspace_id=workspace_id)


HOME_COVERAGE_SYNC_TIMEOUT_S = 5.0


async def _sector_preflight_gate(
    automation: dict[str, Any],
    *,
    automation_id: Optional[str],
    execution_id: str,
    workspace_id: str,
    name: str,
    persist: bool,
) -> Optional[PreflightResult]:
    """The sector branch's gate: a skip result, or None to go on.

    Called before any early return that would otherwise wake the task
    (``autonomy.enabled=false`` passes everything through), so a leftover
    sector task never runs with the flag off or on a stale binding.
    """
    # 行业分支整体门控（r3 lead 决定）：开关关闭时，遗留的 sector_* 任务
    # 直接跳过——不查注册表、不读不写任何 watch state，原因只记预检
    # 日志。开关打开时复查单元绑定；失配（换绑/归档/非 sector/查不到/
    # 查询失败）同样只跳过本次：不写 watch_state——对一个可能已改作
    # 公司用途的旧工作区 upsert sector 主体行会破坏其身份（codex r3
    # #1）——也不暂停任务：暂停是 tracking.disable 的职责，它按
    # unit_id 覆盖跨工作区任务；这里基于一次性读到的绑定做处置会和
    # 绑定/启用交错误停刚恢复的任务（codex r3 #2）。
    from src.dalton.settings import is_coverage_units_enabled

    if not is_coverage_units_enabled():
        return await _skip_sector_run(
            automation_id=automation_id,
            execution_id=execution_id,
            workspace_id=workspace_id,
            name=name,
            reason="coverage_units_disabled",
            persist=persist,
        )
    from src.dalton.autonomy.sector import check_sector_automation_binding

    binding_reason, _binding_unit = await check_sector_automation_binding(automation)
    if binding_reason is not None:
        logger.warning(
            "[DALTON_PREFLIGHT] sector automation %s skipped: %s",
            name,
            binding_reason,
        )
        return await _skip_sector_run(
            automation_id=automation_id,
            execution_id=execution_id,
            workspace_id=workspace_id,
            name=name,
            reason=binding_reason,
            persist=persist,
        )

    return None


async def _skip_sector_run(
    *,
    automation_id: Optional[str],
    execution_id: str,
    workspace_id: str,
    name: str,
    reason: str,
    persist: bool,
) -> PreflightResult:
    """Skip a sector automation for ``reason`` — preflight log only, no side effects.

    跳过原因只落 ``dalton_preflight_log``：不写 watch_state（不得覆盖旧
    工作区的主体行）、不暂停任务（tracking.disable 的职责）。零 token、
    零沙箱，下一次调度自然重试。
    """
    logger.info(
        "[DALTON_PREFLIGHT] sector automation %s skipped: %s",
        name,
        reason,
    )
    if persist:
        try:
            await watch_db.insert_preflight_log(
                automation_id=automation_id,
                automation_execution_id=execution_id,
                workspace_id=workspace_id,
                decision="skip",
                reason=reason,
                signals=[],
            )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
    return PreflightResult(
        should_wake=False,
        decision="skip",
        reason=reason,
        signals=[],
        wake_context=None,
    )


async def _sync_home_coverage_for_preflight(
    *,
    user_id: str,
    kind: str,
    ticker: Optional[str],
    persist: bool = True,
    sync_timeout_s: float = HOME_COVERAGE_SYNC_TIMEOUT_S,
) -> Optional[str]:
    """Render `coverage.yaml` into Home before any step that could time out and wake.

    Company execution renders the internal/admin union and does not refresh
    member groups or create member calendar tasks. Personal calendar refresh
    is unchanged. Both paths keep the existing sync timeout.
    """
    # 行业自动化（P3）没有 ticker，但它是工作区级任务而非 Home 任务，
    # 不能落进 "not ticker ⇒ home" 的旧判据里去刷 Home 的 coverage.yaml。
    is_home_dalton = (
        kind in ("calendar", "director_daily", "director_weekly") or not ticker
    ) and not str(kind).startswith("sector_")
    if not is_home_dalton or not persist:
        return None

    company_execution = is_company_system_user(user_id)
    eff_sync_timeout = max(0.05, float(sync_timeout_s or HOME_COVERAGE_SYNC_TIMEOUT_S))
    group_refresh_summary: Optional[str] = None
    # Company execution must not refresh member groups or create member calendar tasks.
    if kind == "calendar" and not company_execution:
        try:
            refresh_res = await asyncio.wait_for(
                refresh_due_coverage_groups_for_preflight(
                    user_id,
                    sync_home=False,
                ),
                timeout=eff_sync_timeout,
            )
            if isinstance(refresh_res, dict) and refresh_res.get("summary_text"):
                group_refresh_summary = str(refresh_res["summary_text"])
        except asyncio.TimeoutError:
            logger.warning(
                "[DALTON_PREFLIGHT] refresh_due_coverage_groups_for_preflight timed out after %.2fs for user=%s",
                eff_sync_timeout,
                user_id,
            )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] refresh_due_coverage_groups_for_preflight failed: %s", e)

    async def _render_and_sync() -> None:
        if company_execution:
            yaml_text = await render_company_coverage_yaml()
        else:
            yaml_text = await render_coverage_yaml(user_id)
        await sync_user_coverage_to_home(user_id, yaml_text=yaml_text)

    try:
        await asyncio.wait_for(_render_and_sync(), timeout=eff_sync_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "[DALTON_PREFLIGHT] sync_user_coverage_to_home timed out after %.2fs for user=%s",
            eff_sync_timeout,
            user_id,
        )
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] sync_user_coverage_to_home failed: %s", e)

    return group_refresh_summary


# Company-watch probe order: local due checks first, then network filings.
# A network hang must not skip an earnings date or a due agenda item.
_COMPANY_LOCAL_PROBES: tuple[str, ...] = ("agenda", "earnings")
_COMPANY_NETWORK_PROBES: tuple[str, ...] = ("sec", "cnhk")
_COMPANY_PROBE_ORDER: tuple[str, ...] = _COMPANY_LOCAL_PROBES + _COMPANY_NETWORK_PROBES
_PREFLIGHT_DECISIONS = frozenset({"wake", "skip", "passthrough"})
# Filing watermarks. A later full-document write must not keep a probe's new
# values when that probe's signals failed to save.
_SEEN_WATERMARK_KEYS: tuple[str, ...] = (
    "seen_accessions",
    "last_sec_accession",
    "seen_cnhk_filings",
    "last_cnhk_check_date",
)


def _mark_probe_done(progress: Optional[dict[str, Any]], probe_name: str) -> None:
    if not isinstance(progress, dict):
        return
    pending = progress.get("pending")
    if isinstance(pending, list):
        progress["pending"] = [name for name in pending if name != probe_name]


def _note_probe_not_succeeded(progress: Optional[dict[str, Any]]) -> None:
    """A backed-off or failed probe did not finish a real request."""
    if isinstance(progress, dict):
        progress["probes_all_succeeded"] = False


def _restore_seen_watermarks(state: dict[str, Any], prior: dict[str, Any]) -> dict[str, Any]:
    """Drop a probe's new filing watermarks so a later write cannot mark them read."""
    restored = dict(state)
    for key in _SEEN_WATERMARK_KEYS:
        if key in prior:
            restored[key] = prior[key]
        else:
            restored.pop(key, None)
    return restored


async def _upsert_company_watch_state(
    *,
    persist: bool,
    state_read_ok: bool,
    workspace_id: Optional[str],
    ticker: Optional[str],
    state: dict[str, Any],
) -> None:
    """Write company watch state only after a successful read.

    A failed read must not replace the stored document with an empty one.
    The SQL keeps ``preflight_timeout_streaks`` from the stored row.
    """
    if not persist or not workspace_id or not ticker:
        return
    if not state_read_ok:
        logger.warning("[DALTON_PREFLIGHT] 跟踪状态读取失败，跳过写回以免覆盖已有记录")
        return
    try:
        await watch_db.upsert_watch_state(workspace_id, ticker, state)
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] upsert_watch_state failed: %s", e)


async def _commit_network_probe_signals(
    *,
    signals: list[ProbeSignal],
    state: dict[str, Any],
    persist: bool,
    state_read_ok: bool,
    workspace_id: Optional[str],
    ticker: Optional[str],
    saved_out: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Save one network probe's signals. The seen watermark stays in ``state``.

    The run writes watch state once, at the end. A ``False`` return tells the
    caller to roll ``state``'s filing watermarks back first, so that write
    cannot mark a filing read while its signal is missing. ``state`` and
    ``state_read_ok`` are kept for that caller; this function does not write them.
    """
    del state, state_read_ok
    if not persist:
        return True
    saved_rows: list[dict[str, Any]] = []
    saved_all = True
    for sig in signals:
        try:
            row, _should_wake = await watch_db.upsert_signal(
                workspace_id=workspace_id,
                ticker=ticker,
                probe=sig.probe,
                signal_key=sig.signal_key,
                priority=sig.priority,
                summary=sig.summary,
                payload=sig.payload,
            )
            saved_rows.append(
                {
                    "signal_id": str((row or {}).get("signal_id") or ""),
                    "probe": sig.probe,
                    "signal_key": sig.signal_key,
                    "priority": sig.priority,
                    "summary": sig.summary,
                    "payload": sig.payload,
                }
            )
        except Exception as e:
            saved_all = False
            logger.warning("[DALTON_PREFLIGHT] upsert_signal failed: %s", e)
    if not saved_all:
        logger.warning("[DALTON_PREFLIGHT] 网络探针信号未全部保存，不更新已读记录")
        return False
    if saved_out is not None:
        saved_out.extend(saved_rows)
    return True


def _merge_wake_results(
    first: Optional[PreflightResult],
    second: Optional[PreflightResult],
    *,
    name: str,
    ticker: Optional[str],
) -> Optional[PreflightResult]:
    """Combine two wake decisions. A skip does not erase an earlier wake."""
    wakes = [item for item in (first, second) if item is not None and item.should_wake]
    if not wakes:
        return second if second is not None else first
    if len(wakes) == 1:
        return wakes[0]
    signals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    incomplete: list[str] = []
    for item in wakes:
        for probe_name in item.incomplete_probes:
            if probe_name not in incomplete:
                incomplete.append(probe_name)
        for sig in item.signals:
            pair = (str(sig.get("probe") or ""), str(sig.get("signal_key") or ""))
            if not pair[1] or pair in seen:
                continue
            seen.add(pair)
            signals.append(sig)
    return PreflightResult(
        should_wake=True,
        decision="wake",
        reason=f"{len(signals)}_signals",
        signals=signals,
        wake_context=format_wake_directive(
            automation_name=name,
            ticker=ticker,
            signals=signals,
        ),
        incomplete_probes=incomplete,
    )


def _remember_wake(
    progress: Optional[dict[str, Any]],
    result: PreflightResult,
    *,
    name: str,
    ticker: Optional[str],
) -> None:
    """Store a claimed wake where cancellation of a later await cannot drop it."""
    if not isinstance(progress, dict) or not result.should_wake:
        return
    existing = progress.get("committed_wake")
    if isinstance(existing, PreflightResult) and existing.should_wake:
        merged = _merge_wake_results(existing, result, name=name, ticker=ticker)
        result = merged if merged is not None else result
    progress["committed_wake"] = result
    progress["wake_log_pending"] = True


def _wake_already_committed(progress: Optional[dict[str, Any]]) -> bool:
    """True when an earlier pass already claimed a wake that must be delivered."""
    if not isinstance(progress, dict):
        return False
    existing = progress.get("committed_wake")
    return isinstance(existing, PreflightResult) and existing.should_wake


def _timeout_streak_entry(
    state: dict[str, Any],
    automation_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """This task's timeout entry, or None when it is missing or still a bare int."""
    raw = state.get("preflight_timeout_streaks")
    if not isinstance(raw, dict):
        return None
    key = str(automation_id or "").strip() or "_"
    entry = raw.get(key)
    if isinstance(entry, dict):
        return entry
    return None


def _pending_timeout_reminder(
    state: dict[str, Any],
    automation_id: Optional[str],
    threshold: int,
) -> Optional[ProbeSignal]:
    """A reminder for this cycle, read at the start of a run.

    The sentence uses the streak stored with the cycle. A bare integer or a
    blank cycle start is filled by the timeout bump; this read waits until
    that cycle id exists. ``reminded`` stops a second reminder for the same
    cycle. The caller claims this signal in the one conclude step. A timeout
    before that conclude leaves the cycle unreminded, and the next run tries
    again.
    """
    entry = _timeout_streak_entry(state, automation_id)
    if entry is None:
        return None
    try:
        streak = int(entry.get("streak") or 0)
    except (TypeError, ValueError):
        return None
    step = threshold if threshold >= 1 else 1
    if streak < step or entry.get("reminded") is True:
        return None
    cycle = entry.get("cycle_started_at")
    if cycle is None:
        return None
    cycle_s = str(cycle).strip()
    if not cycle_s or cycle_s.lower() == "null":
        return None
    return ProbeSignal(
        probe="preflight",
        signal_key=f"preflight_timeout_streak:{automation_id or '_'}:{cycle_s}",
        priority="P1",
        summary=f"预检连续 {streak} 次超时，需要检查数据源",
        payload={
            "kind": "preflight_timeout_streak",
            "streak": streak,
            "cycle_started_at": cycle_s,
        },
    )


def _reminder_cycle(sig: ProbeSignal) -> str:
    payload = sig.payload if isinstance(sig.payload, dict) else {}
    if payload.get("kind") != "preflight_timeout_streak":
        return ""
    return str(payload.get("cycle_started_at") or "").strip()


def _note_timeout_reminder_settled(
    probe_progress: Optional[dict[str, Any]],
    cycle_started_at: str,
    *,
    persist: bool,
) -> None:
    """Remember a cycle whose reminder was claimed or was already woken."""
    if not persist or not cycle_started_at or not isinstance(probe_progress, dict):
        return
    probe_progress["timeout_reminder_settled"] = cycle_started_at


_TIMEOUT_REMINDER_RETRY_LIMIT = 5


def _signal_was_claimed(row: Any) -> bool:
    """True when an execution has already claimed this signal row."""
    if not isinstance(row, dict):
        return False
    if str(row.get("status") or "").strip().lower() == "woken":
        return True
    if row.get("woken_execution_id") not in (None, ""):
        return True
    return row.get("woken_at") is not None


def _signal_closed_unclaimed(row: Any) -> bool:
    """True when the row expired and nobody claimed it.

    A row someone resolved was seen and dismissed, so it settles the cycle
    rather than starting a retry.
    """
    if not isinstance(row, dict) or _signal_was_claimed(row):
        return False
    status = str(row.get("status") or "").strip().lower()
    if status == "expired":
        return True
    if row.get("expired_at") is not None and row.get("resolved_at") is None:
        return True
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return payload.get("status") == "expired"


def _signal_resolved(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip().lower()
    return status == "resolved" or row.get("resolved_at") is not None


async def _save_timeout_reminder_retry(
    sig: ProbeSignal,
    *,
    cycle: str,
    persist: bool,
    workspace_id: Optional[str],
    ticker: Optional[str],
    probe_progress: Optional[dict[str, Any]],
    pending_reminder_cycles: dict[str, str],
    candidate_signals: list[dict[str, Any]],
) -> None:
    """Save a fresh reminder row after the previous one closed unclaimed.

    The new key keeps the cycle id and adds ``:retry:N``. A row that comes
    back already claimed settles the cycle. A still-pending row is claimed
    with the rest of this run. Each closed slot tries the next suffix, up to
    a fixed limit. Failing to save, or finding no claimable row, leaves the
    cycle unreminded so the next finished run tries again.
    """
    # The fixed suffixes keep keys stable across runs. Once all of them have
    # closed unclaimed, each later run adds one key of its own (UTC second), so
    # a cycle is never stuck without a claimable reminder; still one attempt
    # per run.
    run_key = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    attempts = [str(n) for n in range(1, _TIMEOUT_REMINDER_RETRY_LIMIT + 1)] + [run_key]
    for attempt in attempts:
        retry_key = f"{sig.signal_key}:retry:{attempt}"
        try:
            retry_row, retry_unwoken = await watch_db.upsert_signal(
                workspace_id=workspace_id,
                ticker=ticker,
                probe=sig.probe,
                signal_key=retry_key,
                priority=sig.priority,
                summary=sig.summary,
                payload=sig.payload,
            )
        except Exception as retry_err:
            logger.warning("[DALTON_PREFLIGHT] 连续超时提醒重试保存失败: %s", retry_err)
            return
        if retry_unwoken:
            signal_id = str((retry_row or {}).get("signal_id") or "").strip()
            if not signal_id:
                logger.warning("[DALTON_PREFLIGHT] 连续超时提醒重试没有信号编号，本周期下次再试")
                return
            pending_reminder_cycles[signal_id] = cycle
            candidate_signals.append(
                {
                    "signal_id": signal_id,
                    "probe": sig.probe,
                    "signal_key": retry_key,
                    "priority": sig.priority,
                    "summary": sig.summary,
                    "payload": sig.payload,
                }
            )
            return
        if _signal_was_claimed(retry_row) or _signal_resolved(retry_row):
            _note_timeout_reminder_settled(probe_progress, cycle, persist=persist)
            return
        if not _signal_closed_unclaimed(retry_row):
            return
    logger.warning("[DALTON_PREFLIGHT] 连续超时提醒仍未领取，本周期下次再试")


async def _conclude_preflight(
    *,
    emitted: list[ProbeSignal],
    state: dict[str, Any],
    persist: bool,
    workspace_id: Optional[str],
    ticker: Optional[str],
    automation_id: Optional[str],
    execution_id: str,
    name: str,
    user_id: Optional[str],
    agenda_doc_override: Optional[dict[str, Any]],
    watch_state_override: Optional[dict[str, Any]],
    group_refresh_summary: Optional[str],
    load_saved_signals: bool,
    probe_progress: Optional[dict[str, Any]] = None,
    triage_timeout_s: Optional[float] = None,
) -> PreflightResult:
    """Upsert, triage, and claim ``emitted``. This is the only claim in a preflight.

    The caller runs it after the probe-phase timeout, so that timeout cannot
    cancel a claim that has already started.

    Once ``claim_signals_woken`` returns, the wake is stored on
    ``probe_progress`` before any later await. The caller writes the wake log
    after this function returns, so a log failure cannot drop a claimed wake.
    """
    candidate_signals: list[dict[str, Any]] = []
    pending_reminder_cycles: dict[str, str] = {}

    if persist:
        for sig in emitted:
            try:
                row, is_unwoken = await watch_db.upsert_signal(
                    workspace_id=workspace_id,
                    ticker=ticker,
                    probe=sig.probe,
                    signal_key=sig.signal_key,
                    priority=sig.priority,
                    summary=sig.summary,
                    payload=sig.payload,
                )
                cycle = _reminder_cycle(sig)
                if cycle and is_unwoken:
                    pending_reminder_cycles[str(row.get("signal_id") or "")] = cycle
                elif cycle:
                    # should_wake is false for a claimed row and also for an
                    # expired or resolved row. A claimed row, or one a person
                    # resolved, counts as reminded. An expired row that was
                    # never claimed is saved again under a new key; this run's
                    # claim marks the cycle.
                    if _signal_was_claimed(row) or _signal_resolved(row):
                        _note_timeout_reminder_settled(
                            probe_progress,
                            cycle,
                            persist=persist,
                        )
                    elif _signal_closed_unclaimed(row):
                        await _save_timeout_reminder_retry(
                            sig,
                            cycle=cycle,
                            persist=persist,
                            workspace_id=workspace_id,
                            ticker=ticker,
                            probe_progress=probe_progress,
                            pending_reminder_cycles=pending_reminder_cycles,
                            candidate_signals=candidate_signals,
                        )
                if is_unwoken:
                    candidate_signals.append(
                        {
                            "signal_id": str(row.get("signal_id") or ""),
                            "probe": sig.probe,
                            "signal_key": sig.signal_key,
                            "priority": sig.priority,
                            "summary": sig.summary,
                            "payload": sig.payload,
                        }
                    )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] upsert_signal failed: %s", e)
                # A reminder that did not save stays unreminded and is retried
                # on the next finished run. Other signals keep the old fallback.
                if not _reminder_cycle(sig):
                    candidate_signals.append(sig.to_dict())
    else:
        seen_keys: set[tuple[str, str]] = set(
            tuple(x) for x in (state.get("_woken_signal_keys") or []) if isinstance(x, (list, tuple)) and len(x) == 2
        )
        for sig in emitted:
            pair = (sig.probe, sig.signal_key)
            if pair not in seen_keys:
                candidate_signals.append(sig.to_dict())
                # The stale-todo sweep-up is only recorded once it actually
                # rides along a wake (below); recording it here would let a
                # lone, non-waking stale hint consume the week's dedup key.
                if (sig.payload or {}).get("kind") != "stale_todos":
                    seen_keys.add(pair)
        state["_woken_signal_keys"] = [list(x) for x in sorted(seen_keys)]
        if watch_state_override is not None:
            watch_state_override.clear()
            watch_state_override.update(state)

    if persist and load_saved_signals and workspace_id:
        try:
            saved_rows = await watch_db.list_unwoken_signals(workspace_id, ticker)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] list unwoken signals failed: %s", e)
            saved_rows = []
        have = {
            (str(item.get("probe") or ""), str(item.get("signal_key") or ""))
            for item in candidate_signals
        }
        for row in saved_rows:
            pair = (str(row.get("probe") or ""), str(row.get("signal_key") or ""))
            if not pair[0] or not pair[1] or pair in have:
                continue
            have.add(pair)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            candidate_signals.append(
                {
                    "signal_id": str(row.get("signal_id") or ""),
                    "probe": pair[0],
                    "signal_key": pair[1],
                    "priority": row.get("priority") or "P1",
                    "summary": row.get("summary") or "",
                    "payload": payload,
                }
            )

    # The stale-todo sweep-up never stands in the wake decision (round 4 #3):
    # it is kept out of the direct-wake split, the LLM triage and thereby the
    # triage fallback, so a lone stale signal reads as "no new signals" and
    # stays unhandled for the next run (its ISO-week signal_key still dedups
    # the hint to once a week). It only rides along — and only then is marked
    # notified — when another signal actually wakes this round.
    stale_signals: list[dict[str, Any]] = []
    effective_candidates: list[dict[str, Any]] = []
    for cand in candidate_signals:
        payload = cand.get("payload") if isinstance(cand.get("payload"), dict) else {}
        if payload.get("kind") == "stale_todos":
            stale_signals.append(cand)
        else:
            effective_candidates.append(cand)
    candidate_signals = effective_candidates

    if not candidate_signals:
        if persist and not _wake_already_committed(probe_progress):
            try:
                await watch_db.insert_preflight_log(
                    automation_id=automation_id,
                    automation_execution_id=execution_id,
                    workspace_id=workspace_id,
                    decision="skip",
                    reason="no_new_signals",
                    signals=[],
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason="no_new_signals",
            signals=[],
            wake_context=None,
        )

    # Split into direct-wake signals and LLM-triage signals (G4.3)
    direct_signals: list[dict[str, Any]] = []
    triage_candidates: list[dict[str, Any]] = []
    for cand in candidate_signals:
        if is_direct_wake_signal(cand):
            direct_signals.append(cand)
        else:
            triage_candidates.append(cand)

    triage_kept: list[dict[str, Any]] = []
    triage_skipped: list[dict[str, Any]] = []
    triage_reason = "no_triage"
    if triage_candidates:
        try:
            triage_call = _triage_signals_with_llm(
                ticker=ticker,
                workspace_id=workspace_id,
                user_id=user_id,
                signals=triage_candidates,
                agenda_doc=agenda_doc_override,
                watch_state=state,
                persist=persist,
            )
            if triage_timeout_s is not None:
                triage_kept, triage_skipped, triage_reason = await asyncio.wait_for(
                    triage_call,
                    timeout=triage_timeout_s,
                )
            else:
                triage_kept, triage_skipped, triage_reason = await triage_call
        except asyncio.TimeoutError:
            logger.warning("[DALTON_PREFLIGHT] 历史披露分诊超时，本轮领取直接信号")
            triage_kept = []
            triage_skipped = []
            triage_reason = "triage_timeout"
        if persist and triage_skipped:
            skipped_ids = [str(s["signal_id"]) for s in triage_skipped if s.get("signal_id")]
            if skipped_ids:
                try:
                    await watch_db.resolve_signals(
                        skipped_ids,
                        workspace_id=workspace_id,
                        ticker=ticker,
                        resolved_by="preflight.triage:skip",
                    )
                except Exception as e:
                    logger.warning("[DALTON_PREFLIGHT] resolve triage-skipped signals failed: %s", e)

    woke_signals = direct_signals + triage_kept

    if not woke_signals:
        skip_reason = triage_reason if triage_candidates else "no_new_signals"
        if persist and not _wake_already_committed(probe_progress):
            try:
                await watch_db.insert_preflight_log(
                    automation_id=automation_id,
                    automation_execution_id=execution_id,
                    workspace_id=workspace_id,
                    decision="skip",
                    reason=skip_reason,
                    signals=triage_skipped,
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason=skip_reason,
            signals=[],
            wake_context=None,
        )

    # Atomically claim unwoken signals (A10): filter out any signal already claimed by a concurrent execution
    signal_ids_to_mark = [str(s["signal_id"]) for s in woke_signals if s.get("signal_id")]
    if persist and signal_ids_to_mark:
        try:
            claimed_raw = await watch_db.claim_signals_woken(
                signal_ids_to_mark,
                execution_id,
                workspace_id=workspace_id,
            )
            claimed_ids = {
                str(r.get("signal_id") if isinstance(r, dict) else r)
                for r in (claimed_raw or [])
                if (r.get("signal_id") if isinstance(r, dict) else r)
            }
            for signal_id, cycle in pending_reminder_cycles.items():
                if signal_id and signal_id in claimed_ids:
                    _note_timeout_reminder_settled(
                        probe_progress,
                        cycle,
                        persist=persist,
                    )
            woke_signals = [
                s
                for s in woke_signals
                if not s.get("signal_id") or str(s["signal_id"]) in claimed_ids
            ]
            if not woke_signals:
                if persist and not _wake_already_committed(probe_progress):
                    try:
                        await watch_db.insert_preflight_log(
                            automation_id=automation_id,
                            automation_execution_id=execution_id,
                            workspace_id=workspace_id,
                            decision="skip",
                            reason="already_claimed_by_concurrent_run",
                            signals=[],
                        )
                    except Exception as e:
                        logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
                return PreflightResult(
                    should_wake=False,
                    decision="skip",
                    reason="already_claimed_by_concurrent_run",
                    signals=[],
                    wake_context=None,
                )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] claim_signals_woken failed: %s", e)
            if persist and not _wake_already_committed(probe_progress):
                try:
                    await watch_db.insert_preflight_log(
                        automation_id=automation_id,
                        automation_execution_id=execution_id,
                        workspace_id=workspace_id,
                        decision="skip",
                        reason="claim_failed",
                        signals=[],
                    )
                except Exception as log_err:
                    logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", log_err)
            return PreflightResult(
                should_wake=False,
                decision="skip",
                reason="claim_failed",
                signals=[],
                wake_context=None,
            )

    # A real wake happened: the stale-todo sweep-up rides along, and only then
    # is it marked notified (round 4 #3). Record that decision before the
    # resolve await. Cancellation of the resolve cannot drop the claim.
    if stale_signals:
        if not persist:
            keys = {
                tuple(x)
                for x in (state.get("_woken_signal_keys") or [])
                if isinstance(x, (list, tuple)) and len(x) == 2
            }
            keys.update(
                (str(s.get("probe")), str(s.get("signal_key"))) for s in stale_signals
            )
            state["_woken_signal_keys"] = [list(x) for x in sorted(keys)]
            if watch_state_override is not None:
                watch_state_override.clear()
                watch_state_override.update(state)
        woke_signals = woke_signals + stale_signals

    wake_ctx = format_wake_directive(
        automation_name=name,
        ticker=ticker,
        signals=woke_signals,
    )
    if group_refresh_summary:
        safe_refresh = _sanitize_untrusted_wake_field(group_refresh_summary, max_len=1500)
        wake_ctx = f"{wake_ctx}\n<coverage_group_refresh>\n{safe_refresh}\n</coverage_group_refresh>"
    result = PreflightResult(
        should_wake=True,
        decision="wake",
        reason=f"{len(woke_signals)}_signals",
        signals=woke_signals,
        wake_context=wake_ctx,
    )
    _remember_wake(probe_progress, result, name=name, ticker=ticker)
    if persist and stale_signals:
        stale_ids = [str(s["signal_id"]) for s in stale_signals if s.get("signal_id")]
        if stale_ids:
            try:
                await watch_db.resolve_signals(
                    stale_ids,
                    workspace_id=workspace_id,
                    ticker=ticker,
                    resolved_by="preflight.stale_todos:notified",
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] resolve stale_todos signals failed: %s", e)
    return result


def _conclude_kwargs(
    *,
    emitted: list[ProbeSignal],
    state: dict[str, Any],
    persist: bool,
    workspace_id: Optional[str],
    ticker: Optional[str],
    automation_id: Optional[str],
    execution_id: str,
    name: str,
    user_id: Optional[str],
    agenda_doc_override: Optional[dict[str, Any]],
    watch_state_override: Optional[dict[str, Any]],
    group_refresh_summary: Optional[str],
    load_saved_signals: bool,
    probe_progress: Optional[dict[str, Any]] = None,
    triage_timeout_s: Optional[float] = None,
) -> dict[str, Any]:
    return {
        "emitted": emitted,
        "state": state,
        "persist": persist,
        "workspace_id": workspace_id,
        "ticker": ticker,
        "automation_id": automation_id,
        "execution_id": execution_id,
        "name": name,
        "user_id": user_id,
        "agenda_doc_override": agenda_doc_override,
        "watch_state_override": watch_state_override,
        "group_refresh_summary": group_refresh_summary,
        "load_saved_signals": load_saved_signals,
        "probe_progress": probe_progress,
        "triage_timeout_s": triage_timeout_s,
    }


async def _preflight_check_inner(
    automation: dict[str, Any],
    execution_id: str,
    ownership: _VerifiedAutomationOwnership,
    *,
    watch_state_override: Optional[dict[str, Any]] = None,
    agenda_doc_override: Optional[dict[str, Any]] = None,
    persist: bool = True,
    home_synced_in_outer: bool = False,
    precomputed_group_refresh_summary: Optional[str] = None,
    probe_progress: Optional[dict[str, Any]] = None,
) -> PreflightResult:
    """Inner implementation of `preflight_check` wrapped by `asyncio.wait_for` (A3)."""
    if not is_dalton_automation(automation):
        return PreflightResult(should_wake=True, decision="passthrough", reason="non_dalton")

    if not isinstance(ownership, _VerifiedAutomationOwnership):
        raise TypeError("ownership must be a _VerifiedAutomationOwnership instance")
    user_id = ownership.user_id
    workspace_id = ownership.workspace_id

    settings = get_dalton_settings()
    automation_id = str(automation.get("automation_id") or "") or None
    name = str(automation.get("name") or "dalton-task")
    ticker = _extract_ticker(automation)
    kind = _determine_kind(automation)

    if str(kind).startswith("sector_"):
        gated = await _sector_preflight_gate(
            automation,
            automation_id=automation_id,
            execution_id=execution_id,
            workspace_id=workspace_id,
            name=name,
            persist=persist,
        )
        if gated is not None:
            return gated

    if kind == "data_health":
        from src.dalton.data_map.health import run_all_health_checks

        health_summary_text = "体检完成"
        health_payload: dict[str, Any] = {}
        try:
            res = await run_all_health_checks(persist=persist)
            if isinstance(res, dict):
                avail = res.get("available_count", 0)
                checked = res.get("checked_count", 0)
                unavail = res.get("unavailable_count", 0)
                health_summary_text = (
                    f"体检完成：可用 {avail}/{checked}（不可用 {unavail}）"
                )
                health_payload = {
                    "checked_count": checked,
                    "available_count": avail,
                    "unavailable_count": unavail,
                }
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            logger.warning("[DALTON_PREFLIGHT] run_all_health_checks failed: %s", exc)
            err_detail = str(exc).strip()
            err_summary = f"{type(exc).__name__}: {err_detail}" if err_detail else type(exc).__name__
            health_summary_text = f"体检失败：{err_summary}"[:160]
            health_payload = {"error": type(exc).__name__}

        if persist:
            try:
                await watch_db.insert_preflight_log(
                    automation_id=automation_id,
                    automation_execution_id=execution_id,
                    workspace_id=workspace_id,
                    decision="skip",
                    reason=health_summary_text,
                    signals=[health_payload] if health_payload else [],
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)

        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason=health_summary_text,
            signals=[],
            wake_context=None,
        )

    group_refresh_summary = precomputed_group_refresh_summary
    if not home_synced_in_outer:
        group_refresh_summary = await _sync_home_coverage_for_preflight(
            user_id=user_id,
            kind=kind,
            ticker=ticker,
            persist=persist,
        )

    if not settings.autonomy.enabled:
        return PreflightResult(should_wake=True, decision="passthrough", reason="autonomy_disabled")

    try:
        tz = ZoneInfo(settings.autonomy.timezone)
    except Exception:
        tz = timezone.utc

    # Price triggers are already event-driven by upstream price monitor
    if automation.get("trigger_type") == "price" or kind == "price_move":
        sig = {
            "probe": "price",
            "signal_key": f"trigger:{execution_id[:8]}",
            "priority": "P0",
            "summary": f"{ticker or name} 触发价格异动监控阈值",
        }
        wake_ctx = format_wake_directive(automation_name=name, ticker=ticker, signals=[sig])
        return PreflightResult(
            should_wake=True,
            decision="wake",
            reason="price_trigger",
            signals=[sig],
            wake_context=wake_ctx,
        )

    # Check shared daily USD cap before running probes or waking sandbox (A1)
    budget_ok, budget_reason = (True, None)
    if persist:
        budget_ok, budget_reason = await _check_budget_available(user_id=user_id)
    elif isinstance(watch_state_override, dict) and watch_state_override.get("budget_exhausted"):
        budget_ok, budget_reason = (False, "budget_exhausted")
    if not budget_ok:
        await _defer_once_automation_on_budget_exhausted(automation, persist=persist)
        if persist:
            try:
                await watch_db.insert_preflight_log(
                    automation_id=automation_id,
                    automation_execution_id=execution_id,
                    workspace_id=workspace_id,
                    decision="skip",
                    reason=budget_reason,
                    signals=[],
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason=budget_reason,
            signals=[],
            wake_context=None,
        )

    # Expire unresolved signals older than 7 days (G4.2 / A14)
    if persist:
        try:
            await watch_db.expire_old_signals(max_age_days=7, workspace_id=workspace_id)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] expire_old_signals failed: %s", e)

    # Load watch state. A failed read must not be written back later.
    state: dict[str, Any] = {}
    state_read_ok = True
    if watch_state_override is not None:
        state = dict(watch_state_override)
    elif persist and workspace_id:
        try:
            state = await watch_db.get_watch_state(workspace_id)
        except Exception as e:
            state_read_ok = False
            logger.warning("[DALTON_PREFLIGHT] get_watch_state failed: %s", e)
    if isinstance(probe_progress, dict):
        # A failed read must not clear the timeout streak on the way out.
        probe_progress["state_read_ok"] = state_read_ok

    emitted: list[ProbeSignal] = []

    if kind == "calendar":
        sigs, state = await probe_calendar(watch_state=state)
        if group_refresh_summary:
            if sigs:
                for sig in sigs:
                    sig_pl = dict(sig.payload or {})
                    sig_pl["group_refresh_summary"] = group_refresh_summary
                    sig.payload = sig_pl
            else:
                sigs = [
                    ProbeSignal(
                        probe="calendar",
                        signal_key=f"group_refresh:{datetime.now(tz).date().isoformat()}",
                        priority="P1",
                        summary=_sanitize_untrusted_wake_field(group_refresh_summary, max_len=180),
                        payload={"group_refresh_summary": group_refresh_summary},
                    )
                ]
        emitted.extend(sigs)
    elif kind in ("director_daily", "director_weekly"):
        sigs, state = await probe_director(
            kind=kind,
            watch_state=state,
            persist=persist,
            agenda_docs_override=[agenda_doc_override] if isinstance(agenda_doc_override, dict) else None,
            user_id=user_id,
        )
        emitted.extend(sigs)
    elif kind in ("weekly_delta", "earnings_window", "custom_dalton"):
        # 同一用户、同一代码、同一天可以有多个计划任务（早班/晚班、多个
        # dalton-dash-…）。跟踪状态和信号表按工作区共享，键带上自动化 ID，
        # 每个任务当天各自有一条计划唤醒信号。
        emitted.append(
            ProbeSignal(
                probe="schedule",
                signal_key=f"{kind}:{automation_id}:{datetime.now(tz).date().isoformat()}",
                priority="P1",
                summary=f"{ticker or name} 计划任务唤醒（{kind}）",
                payload={"kind": kind},
            )
        )
    elif kind == "sector_delta":
        # 行业资料增量是计划唤醒：采集在唤醒后的沙箱里按
        # dalton-sector-delta 技能执行，预检不跑任何证券探针。
        # 每个行业工作区只有一个 sector_delta 自动化，去重按工作区隔离，
        # 键保持 sector_delta:日期。
        emitted.append(
            ProbeSignal(
                probe="schedule",
                signal_key=f"sector_delta:{datetime.now(tz).date().isoformat()}",
                priority="P1",
                summary=f"{name} 行业资料增量计划唤醒",
                payload={"kind": kind},
            )
        )
    elif kind == "sector_watch":
        # 行业议程到期：只跑议程探针，到期口径用行业的 research_tz（缺省
        # 退回用户时区，并把生效时区写进跟踪状态，见 autonomy.sector）。
        from src.dalton.autonomy.sector import resolve_sector_due_timezone

        sector_meta = automation.get("metadata") if isinstance(automation.get("metadata"), dict) else {}
        label = str(sector_meta.get("sector_name") or sector_meta.get("sector_slug") or name)
        tz_name: Optional[str] = None
        try:
            # 时区解析只读不写，dry-run（persist=False）也按真实文档解析，
            # 不让预演与实跑的到期口径分叉；解析失败降级用户时区并记日志。
            tz_name, tz_source = await resolve_sector_due_timezone(
                workspace_id=workspace_id,
                user_id=user_id,
            )
            state["sector_due_tz"] = {"tz": tz_name, "source": tz_source}
        except Exception as exc:
            logger.warning(
                "[DALTON_PREFLIGHT] sector due tz resolve failed for %s: %s", name, exc
            )
        sector_due_tz: Optional[ZoneInfo] = None
        if tz_name:
            try:
                sector_due_tz = ZoneInfo(tz_name)
            except Exception:
                sector_due_tz = None
        eff_ws_id = workspace_id if persist else None
        in_bo, nb_iso = is_in_backoff(state, "agenda")
        if in_bo:
            logger.info(
                "[DALTON_PREFLIGHT] probe=agenda sector=%s in time-backoff until %s",
                name,
                nb_iso,
            )
        else:
            try:
                sigs, state = await probe_agenda(
                    workspace_id=eff_ws_id,
                    ticker=label,
                    watch_state=state,
                    agenda_doc=agenda_doc_override,
                    due_tz=sector_due_tz,
                )
                state = clear_probe_backoff(state, "agenda")
                emitted.extend(sigs)
            except Exception as err:
                logger.warning(
                    "[DALTON_PREFLIGHT] probe=agenda sector=%s transient error: %s",
                    name,
                    err,
                )
                state = record_probe_backoff(state, "agenda", str(err))
    else:
        # Company watch: due items and the earnings date first, then SEC / CN-HK.
        # Network probes still run after a due item. The one claim is the
        # conclude step after this probe phase. A timeout here does not claim
        # or wake; the next finished run claims what was saved.
        threshold = int(getattr(settings.autonomy, "preflight_timeout_streak", 3) or 3)
        if state_read_ok:
            reminder = _pending_timeout_reminder(state, automation_id, threshold)
            if reminder is not None:
                emitted.append(reminder)
                if isinstance(probe_progress, dict):
                    probe_progress["timeout_reminder_cycle"] = str(
                        (reminder.payload or {}).get("cycle_started_at") or ""
                    ).strip()
        t_sym = ticker or "UNKNOWN"
        eff_ws_id = workspace_id if persist else None
        sec_supports_user_id = "user_id" in inspect.signature(probe_sec).parameters
        if isinstance(probe_progress, dict):
            probe_progress["pending"] = list(_COMPANY_PROBE_ORDER)
        local_runners = [
            (
                "agenda",
                lambda st: probe_agenda(
                    workspace_id=eff_ws_id,
                    ticker=t_sym,
                    watch_state=st,
                    agenda_doc=agenda_doc_override,
                ),
            ),
            (
                "earnings",
                lambda st: probe_earnings(
                    ticker=t_sym,
                    watch_state=st,
                    workspace_id=eff_ws_id,
                    agenda_doc=agenda_doc_override,
                ),
            ),
        ]
        network_runners = [
            (
                "sec",
                lambda st: (
                    probe_sec(
                        ticker=t_sym,
                        watch_state=st,
                        workspace_id=eff_ws_id,
                        allow_network=persist,
                        user_id=user_id,
                    )
                    if sec_supports_user_id
                    else probe_sec(
                        ticker=t_sym,
                        watch_state=st,
                        workspace_id=eff_ws_id,
                        allow_network=persist,
                    )
                ),
            ),
            (
                "cnhk",
                lambda st: probe_cnhk_filings(
                    ticker=t_sym,
                    watch_state=st,
                    workspace_id=eff_ws_id,
                    allow_network=persist,
                ),
            ),
        ]

        async def _run_company_probe(probe_name: str, runner: Any, *, commit_seen: bool) -> None:
            nonlocal state
            in_bo, nb_iso = is_in_backoff(state, probe_name)
            if in_bo:
                logger.info(
                    "[DALTON_PREFLIGHT] probe=%s ticker=%s in time-backoff until %s",
                    probe_name,
                    t_sym,
                    nb_iso,
                )
                _note_probe_not_succeeded(probe_progress)
                _mark_probe_done(probe_progress, probe_name)
                return
            prior_state = dict(state)
            try:
                sigs, state = await runner(state)
                if probe_name == "cnhk" and state.get("cnhk_probe_degraded"):
                    state = record_probe_backoff(state, probe_name, "cnhk_probe_degraded")
                    _note_probe_not_succeeded(probe_progress)
                else:
                    state = clear_probe_backoff(state, probe_name)
                # Only the probe that just returned. A flag this run cleared
                # stays false and does not block the streak reset.
                if probe_name == "sec" and state.get("sec_probe_degraded") is True:
                    _note_probe_not_succeeded(probe_progress)
                if probe_name == "cnhk" and state.get("cnhk_probe_partial") is True:
                    _note_probe_not_succeeded(probe_progress)
                emitted.extend(sigs)
                if commit_seen:
                    committed = await _commit_network_probe_signals(
                        signals=sigs,
                        state=state,
                        persist=persist,
                        state_read_ok=state_read_ok,
                        workspace_id=workspace_id,
                        ticker=ticker,
                    )
                    if not committed:
                        state = _restore_seen_watermarks(state, prior_state)
                _mark_probe_done(probe_progress, probe_name)
            except Exception as err:
                logger.warning(
                    "[DALTON_PREFLIGHT] probe=%s ticker=%s transient error: %s",
                    probe_name,
                    t_sym,
                    err,
                )
                state = record_probe_backoff(state, probe_name, str(err))
                _note_probe_not_succeeded(probe_progress)
                _mark_probe_done(probe_progress, probe_name)

        for probe_name, runner in local_runners:
            await _run_company_probe(probe_name, runner, commit_seen=False)
        # Due agenda and earnings signals are saved before any network probe.
        # A timeout still does not claim them. The next finished run claims
        # the saved rows even when the local probe no longer emits them.
        # These probes do not advance filing watermarks, so a failed save
        # does not roll state back; the conclude step retries if it runs.
        local_signals = [sig for sig in emitted if sig.probe in _COMPANY_LOCAL_PROBES]
        if local_signals:
            await _commit_network_probe_signals(
                signals=local_signals,
                state=state,
                persist=persist,
                state_read_ok=state_read_ok,
                workspace_id=workspace_id,
                ticker=ticker,
            )
        await _upsert_company_watch_state(
            persist=persist,
            state_read_ok=state_read_ok,
            workspace_id=workspace_id,
            ticker=ticker,
            state=state,
        )
        for probe_name, runner in network_runners:
            await _run_company_probe(probe_name, runner, commit_seen=True)
        if isinstance(probe_progress, dict):
            pending_after = probe_progress.get("pending")
            # Backoff, a degraded or partial result, and probe errors set
            # probes_all_succeeded to false. Those runs leave the streak as it is.
            if (
                isinstance(pending_after, list)
                and not pending_after
                and probe_progress.get("probes_all_succeeded", True) is True
            ):
                probe_progress["probes_completed"] = True

    # Persist updated watch state (including any auto-resolved CIK or watermarks).
    # Skip the write when the read failed so an empty document cannot replace the row.
    if persist and workspace_id and not state_read_ok and (ticker or str(kind).startswith("sector_")):
        logger.warning("[DALTON_PREFLIGHT] 跟踪状态读取失败，跳过写回以免覆盖已有记录")
    elif persist and workspace_id and ticker:
        try:
            await watch_db.upsert_watch_state(workspace_id, ticker, state)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] upsert_watch_state failed: %s", e)
    elif persist and workspace_id and kind.startswith("sector_") and state_read_ok:
        # 行业主体的跟踪状态（d027）：ticker 为 NULL，身份由 unit_id +
        # subject_kind 承载；元数据缺 unit_id 时放弃写入而不是回退成公司行。
        sector_meta = automation.get("metadata") if isinstance(automation.get("metadata"), dict) else {}
        sector_unit_id = str(sector_meta.get("unit_id") or "").strip()
        if sector_unit_id:
            try:
                await watch_db.upsert_watch_state(
                    workspace_id,
                    None,
                    state,
                    subject_kind="sector",
                    unit_id=sector_unit_id,
                )
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] upsert_watch_state(sector) failed: %s", e)
        else:
            logger.warning(
                "[DALTON_PREFLIGHT] sector automation %s carries no unit_id; watch state not persisted",
                name,
            )

    # The claim runs in preflight_check after the probe-phase timeout returns.
    # Company-watch also picks up signals a previous timed-out run already saved.
    conclude_args = _conclude_kwargs(
        emitted=emitted,
        state=state,
        persist=persist,
        workspace_id=workspace_id,
        ticker=ticker,
        automation_id=automation_id,
        execution_id=execution_id,
        name=name,
        user_id=user_id,
        agenda_doc_override=agenda_doc_override,
        watch_state_override=watch_state_override,
        group_refresh_summary=group_refresh_summary,
        load_saved_signals=(kind == "company_watch"),
        probe_progress=probe_progress,
    )
    if isinstance(probe_progress, dict):
        probe_progress["conclude_kwargs"] = conclude_args
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason="await_conclude",
            signals=[],
            wake_context=None,
        )
    settled = await _conclude_preflight(**conclude_args)
    if settled is None:
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason="no_new_signals",
            signals=[],
            wake_context=None,
        )
    return settled


def _allowed_decision(decision: str) -> str:
    """d006 allows only wake, skip, and passthrough. Never persist another value."""
    if decision not in _PREFLIGHT_DECISIONS:
        logger.error("[DALTON_PREFLIGHT] refused decision %r; logging skip", decision)
        return "skip"
    return decision


async def _log_wake_result(
    *,
    automation_id: Optional[str],
    execution_id: str,
    workspace_id: Optional[str],
    result: PreflightResult,
) -> None:
    """Write the wake log. A failure only warns; the wake itself still stands."""
    try:
        # DALTON-HOOK(preflight): persist complete wake_context (with optional coverage_group_refresh block)
        await watch_db.insert_preflight_log(
            automation_id=automation_id,
            automation_execution_id=execution_id,
            workspace_id=workspace_id,
            decision=_allowed_decision(result.decision),
            reason=str(result.reason or ""),
            signals=list(result.signals),
            wake_context=result.wake_context,
        )
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] 唤醒已领取，日志写入失败: %s", e)


async def _reset_company_timeout_streak(
    *,
    kind: str,
    persist: bool,
    workspace_id: Optional[str],
    automation_id: Optional[str],
) -> None:
    """Clear the streak after every company-watch probe has requested and succeeded.

    The caller skips this when the watch-state read failed, when any probe is
    still unfinished, when a probe was in backoff, or when a probe came back
    degraded or partial. Those runs leave the stored count in place.
    """
    if kind != "company_watch" or not persist or not workspace_id:
        return
    try:
        await watch_db.reset_preflight_timeout_streak(workspace_id, automation_id or "")
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] 预检已完成，但超时计数清零失败: %s", e)


async def _company_preflight_timeout(
    *,
    automation: dict[str, Any],
    execution_id: str,
    workspace_id: str,
    ticker: Optional[str],
    name: str,
    persist: bool,
    timeout_s: float,
    probe_progress: dict[str, Any],
) -> PreflightResult:
    """Company-watch probe phase timed out.

    Signals from probes that already finished stay saved, including due
    agenda and earnings signals written before the network probes. This path
    does not claim or wake. The next run that finishes its probes claims
    those saved signals.
    """
    pending = probe_progress.get("pending") if isinstance(probe_progress, dict) else None
    incomplete = [str(item) for item in pending if item] if isinstance(pending, list) else []
    logger.warning(
        "[DALTON_PREFLIGHT] 预检超时（>%.1fs），%s；未完成探针：%s",
        timeout_s,
        name,
        "、".join(incomplete) or "无",
    )
    if persist and workspace_id:
        try:
            await watch_db.bump_preflight_timeout_streak(
                workspace_id,
                str(automation.get("automation_id") or ""),
                ticker,
            )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] 超时计数写入失败，不覆盖跟踪状态: %s", e)
        try:
            await watch_db.insert_preflight_log(
                automation_id=str(automation.get("automation_id") or "") or None,
                automation_execution_id=execution_id,
                workspace_id=workspace_id,
                decision=_allowed_decision("skip"),
                reason="preflight_timeout",
                signals=[{"incomplete_probes": incomplete}],
            )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
    return PreflightResult(
        should_wake=False,
        decision="skip",
        reason="preflight_timeout",
        signals=[],
        wake_context=None,
        incomplete_probes=incomplete,
    )


async def preflight_check(
    automation: dict[str, Any],
    execution_id: str,
    *,
    watch_state_override: Optional[dict[str, Any]] = None,
    agenda_doc_override: Optional[dict[str, Any]] = None,
    persist: bool = True,
    timeout_s: Optional[float] = None,
) -> PreflightResult:
    """Run zero-token preflight. ``preflight_timeout_s`` bounds the probe phase; the claim runs after it."""
    if not is_dalton_automation(automation):
        return PreflightResult(should_wake=True, decision="passthrough", reason="non_dalton")

    settings = get_dalton_settings()
    kind = _determine_kind(automation)
    if timeout_s is not None:
        eff_timeout = float(timeout_s)
    elif kind == "data_health":
        eff_timeout = float(getattr(settings.autonomy, "data_health_timeout_s", 600.0) or 600.0)
    else:
        eff_timeout = float(settings.autonomy.preflight_timeout_s or 45)

    # Workspace ownership check MUST complete BEFORE `autonomy.enabled` and BEFORE "timeout -> conservative wake":
    # if ownership verification times out or fails, refuse execution and never wake.
    try:
        verified_ownership = await asyncio.wait_for(
            _verify_automation_workspace_ownership(automation),
            timeout=eff_timeout,
        )
    except asyncio.TimeoutError:
        name = str(automation.get("name") or "dalton-task")
        logger.warning(
            "[DALTON_PREFLIGHT] workspace ownership verification timed out after %.1fs for %s; refusing to wake",
            eff_timeout,
            name,
        )
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason="workspace_access_timeout",
            signals=[],
            wake_context=None,
        )

    ticker = _extract_ticker(automation)
    precomputed_group_refresh_summary: Optional[str] = None
    home_synced_in_outer = False
    if kind != "data_health":
        precomputed_group_refresh_summary = await _sync_home_coverage_for_preflight(
            user_id=verified_ownership.user_id,
            kind=kind,
            ticker=ticker,
            persist=persist,
        )
        home_synced_in_outer = True

    if kind != "data_health" and not settings.autonomy.enabled:
        # The passthrough below would wake a sector task without its gate;
        # apply the gate first (company tasks pass through exactly as before).
        if str(kind).startswith("sector_"):
            sector_name = str(automation.get("name") or "dalton-task")
            sector_automation_id = str(automation.get("automation_id") or "") or None
            try:
                gated = await asyncio.wait_for(
                    _sector_preflight_gate(
                        automation,
                        automation_id=sector_automation_id,
                        execution_id=execution_id,
                        workspace_id=verified_ownership.workspace_id,
                        name=sector_name,
                        persist=persist,
                    ),
                    timeout=eff_timeout,
                )
            except asyncio.TimeoutError:
                return await _skip_sector_run(
                    automation_id=sector_automation_id,
                    execution_id=execution_id,
                    workspace_id=verified_ownership.workspace_id,
                    name=sector_name,
                    reason="preflight_timeout_sector",
                    persist=persist,
                )
            if gated is not None:
                return gated
        # Disabled mode does not run probes, so the timeout streak stays as it is.
        return PreflightResult(should_wake=True, decision="passthrough", reason="autonomy_disabled")

    probe_progress: dict[str, Any] = {
        "pending": list(_COMPANY_PROBE_ORDER) if kind == "company_watch" else [],
    }
    try:
        result = await asyncio.wait_for(
            _preflight_check_inner(
                automation,
                execution_id,
                verified_ownership,
                watch_state_override=watch_state_override,
                agenda_doc_override=agenda_doc_override,
                persist=persist,
                home_synced_in_outer=home_synced_in_outer,
                precomputed_group_refresh_summary=precomputed_group_refresh_summary,
                probe_progress=probe_progress,
            ),
            timeout=eff_timeout,
        )
    except asyncio.TimeoutError:
        name = str(automation.get("name") or "dalton-task")
        if kind == "data_health":
            timeout_reason = "体检超时"
            logger.warning(
                "[DALTON_PREFLIGHT] data_health preflight timed out after %.1fs for %s; skipping without wake",
                eff_timeout,
                name,
            )
            if persist:
                try:
                    await watch_db.insert_preflight_log(
                        automation_id=str(automation.get("automation_id") or "") or None,
                        automation_execution_id=execution_id,
                        workspace_id=verified_ownership.workspace_id,
                        decision="skip",
                        reason=timeout_reason,
                        signals=[{"error": "TimeoutError", "timeout_s": eff_timeout}],
                    )
                except Exception as e:
                    logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)
            return PreflightResult(
                should_wake=False,
                decision="skip",
                reason=timeout_reason,
                signals=[],
                wake_context=None,
            )
        if str(kind).startswith("sector_"):
            # 行业任务超时不唤醒：卡住的可能正是绑定校验，唤醒一个绑定已失配的
            # 旧工作区任务会把行业回合写进错误的工作区。超时按跳过处理。
            timeout_reason = "preflight_timeout_sector"
            logger.warning(
                "[DALTON_PREFLIGHT] sector preflight timed out after %.1fs for %s; skipping without wake",
                eff_timeout,
                name,
            )
            return await _skip_sector_run(
                automation_id=str(automation.get("automation_id") or "") or None,
                execution_id=execution_id,
                workspace_id=verified_ownership.workspace_id,
                name=name,
                reason=timeout_reason,
                persist=persist,
            )
        if kind == "company_watch":
            return await _company_preflight_timeout(
                automation=automation,
                execution_id=execution_id,
                workspace_id=verified_ownership.workspace_id,
                ticker=_extract_ticker(automation),
                name=name,
                persist=persist,
                timeout_s=eff_timeout,
                probe_progress=probe_progress,
            )
        # Calendar, director, and schedule tasks keep the previous conservative wake.
        ticker = _extract_ticker(automation)
        logger.warning(
            "[DALTON_PREFLIGHT] preflight_check timed out after %.1fs for %s; waking conservatively",
            eff_timeout,
            name,
        )
        timeout_sig = {
            "probe": "preflight",
            "signal_key": f"timeout:{execution_id[:8]}",
            "priority": "P1",
            "summary": f"预检超时（>{eff_timeout:g}s），保守唤醒分析师",
        }
        return PreflightResult(
            should_wake=True,
            decision="wake",
            reason="preflight_timeout",
            signals=[timeout_sig],
            wake_context=format_wake_directive(
                automation_name=name,
                ticker=ticker,
                signals=[timeout_sig],
            ),
        )
    conclude_args = probe_progress.pop("conclude_kwargs", None)
    if isinstance(conclude_args, dict):
        conclude_args["triage_timeout_s"] = eff_timeout
        settled = await _conclude_preflight(**conclude_args)
        if settled is None:
            result = PreflightResult(
                should_wake=False,
                decision="skip",
                reason="no_new_signals",
                signals=[],
                wake_context=None,
            )
        else:
            result = settled
    probes_completed = probe_progress.get("probes_completed") is True
    state_read_ok = probe_progress.get("state_read_ok", True) is not False
    # A reminder that was due and not claimed has to survive until the next
    # finished run. Clearing the cycle here would drop that retry.
    pending_cycle = probe_progress.get("timeout_reminder_cycle")
    reminder_unsettled = (
        isinstance(pending_cycle, str)
        and bool(pending_cycle.strip())
        and probe_progress.get("timeout_reminder_settled") != pending_cycle
    )
    if kind == "company_watch" and probes_completed and state_read_ok and not reminder_unsettled:
        await _reset_company_timeout_streak(
            kind=kind,
            persist=persist,
            workspace_id=verified_ownership.workspace_id,
            automation_id=str(automation.get("automation_id") or "") or None,
        )
    settled_cycle = probe_progress.get("timeout_reminder_settled")
    if (
        kind == "company_watch"
        and persist
        and state_read_ok
        and isinstance(settled_cycle, str)
        and settled_cycle.strip()
    ):
        try:
            await watch_db.mark_preflight_timeout_reminded(
                verified_ownership.workspace_id,
                str(automation.get("automation_id") or ""),
                settled_cycle.strip(),
            )
        except Exception as e:
            logger.warning(
                "[DALTON_PREFLIGHT] 连续超时提醒已领取，但已提醒标记写入失败: %s",
                e,
            )
    if persist and result.should_wake and probe_progress.get("wake_log_pending"):
        await _log_wake_result(
            automation_id=str(automation.get("automation_id") or "") or None,
            execution_id=execution_id,
            workspace_id=verified_ownership.workspace_id,
            result=result,
        )
        probe_progress["wake_log_pending"] = False
    return result


PREFLIGHT_RETRY_NOTIFY_TIMEOUT_S = 30.0
PREFLIGHT_RETRY_NOTIFY_MAX_DURATION_S = 26.0


async def decide(
    automation: dict[str, Any],
    execution_id: str,
) -> Optional[PreflightResult]:
    """Entrypoint called from `AutomationExecutor.execute()` (`DALTON-HOOK(preflight)`).

    Returns `None` for non-Dalton automations, or a `PreflightResult` (`.skip`, `.summary`, `.context`).
    Never raises an exception so preflight bugs cannot disable user automations.
    """
    try:
        if not is_dalton_automation(automation):
            return None
        try:
            from src.dalton.notify.dispatch import retry_failed_notify_deliveries

            await asyncio.wait_for(
                retry_failed_notify_deliveries(
                    limit=5,
                    max_duration_s=PREFLIGHT_RETRY_NOTIFY_MAX_DURATION_S,
                ),
                timeout=PREFLIGHT_RETRY_NOTIFY_TIMEOUT_S,
            )
        except Exception:
            pass
        raw_user_id = automation.get("user_id")
        if not raw_user_id or not str(raw_user_id).strip():
            logger.warning(
                "[DALTON_PREFLIGHT] decide() skipping Dalton automation %r (execution_id=%s): user_id is missing",
                automation.get("name"),
                execution_id,
            )
            return PreflightResult(
                should_wake=False,
                decision="skip",
                reason="missing_user_id",
                signals=[],
            )
        try:
            res = await preflight_check(automation, execution_id)
        except (HTTPException, PermissionError, ValueError) as exc:
            detail = getattr(exc, "detail", str(exc))
            status_code = getattr(exc, "status_code", 403)
            logger.warning(
                "[DALTON_PREFLIGHT] decide() skipping Dalton automation %r (execution_id=%s): workspace %r access denied (%s: %s)",
                automation.get("name"),
                execution_id,
                automation.get("workspace_id"),
                status_code,
                detail,
            )
            return PreflightResult(
                should_wake=False,
                decision="skip",
                reason="workspace_access_denied",
                signals=[],
            )
        if res.decision == "passthrough":
            return None
        return res
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] decide() unexpected error (skipping fail-closed): %s", e)
        return PreflightResult(
            should_wake=False,
            decision="skip",
            reason="preflight_error",
            signals=[],
        )
