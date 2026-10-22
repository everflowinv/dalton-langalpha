"""Zero-token preflight engine for Dalton autonomous tasks (PK.16).

Runs before `transition_execution(..., to="running")` and before sandbox wakeup
in `AutomationExecutor.execute()`. When no actionable signal is found, the
execution settles immediately as `Outcome.PRECHECK_SKIPPED` (`status='skipped'`,
`skip_reason='precheck'`) with zero LLM tokens and zero sandbox startup.
"""

from __future__ import annotations

import asyncio
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
from src.dalton.autonomy.probes.internal import probe_internal
from src.dalton.autonomy.probes.sec import probe_sec
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
    All other SEC forms (8-K without 2.02, Form 4, 13D/G, 6-K, DEF 14A), cnhk P2 filings, and internal docs go to LLM triage.
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

    if probe == "internal":
        return False

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
        "请判断给定的 SEC 披露（非 2.02 的 8-K、Form 4、13D/G、6-K、DEF 14A）或内部研究文献是否具有实质投研价值、"
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
            date_val = _sanitize_untrusted_wake_field(payload.get("date") or "")
            title_val = _sanitize_untrusted_wake_field(payload.get("title") or "")
            link_val = _sanitize_untrusted_wake_field(payload.get("pdf_link") or "")
            lines.append(
                f"     data (untrusted): source={src_val} | date={date_val} | title={title_val} | pdf_link={link_val}"
            )
    if keys_hint:
        lines.append(f"  trigger_signals: [{keys_hint}]")
    lines.append("</dalton_wake>")
    return "\n".join(lines)


def _extract_ticker(automation: dict[str, Any]) -> Optional[str]:
    meta = automation.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("ticker"):
        return str(meta["ticker"]).upper()
    name = str(automation.get("name") or "").strip().lower()
    if name.startswith("dalton-"):
        parts = name.split("-")
        if len(parts) >= 3 and parts[1] not in ("earnings", "director"):
            return parts[1].upper()
    return None


def _determine_kind(automation: dict[str, Any]) -> str:
    meta = automation.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("dalton_kind"):
        return str(meta["dalton_kind"])
    name = str(automation.get("name") or "").strip().lower()
    if name == "dalton-earnings-calendar":
        return "calendar"
    if name == "dalton-director-daily":
        return "director_daily"
    if name == "dalton-director-weekly":
        return "director_weekly"
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


async def _preflight_check_inner(
    automation: dict[str, Any],
    execution_id: str,
    ownership: _VerifiedAutomationOwnership,
    *,
    watch_state_override: Optional[dict[str, Any]] = None,
    agenda_doc_override: Optional[dict[str, Any]] = None,
    persist: bool = True,
) -> PreflightResult:
    """Inner implementation of `preflight_check` wrapped by `asyncio.wait_for` (A3)."""
    if not is_dalton_automation(automation):
        return PreflightResult(should_wake=True, decision="passthrough", reason="non_dalton")

    if not isinstance(ownership, _VerifiedAutomationOwnership):
        raise TypeError("ownership must be a _VerifiedAutomationOwnership instance")
    user_id = ownership.user_id
    workspace_id = ownership.workspace_id

    settings = get_dalton_settings()
    if not settings.autonomy.enabled:
        return PreflightResult(should_wake=True, decision="passthrough", reason="autonomy_disabled")

    automation_id = str(automation.get("automation_id") or "") or None
    name = str(automation.get("name") or "dalton-task")
    ticker = _extract_ticker(automation)
    kind = _determine_kind(automation)

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

    # Load watch state
    state: dict[str, Any] = {}
    if watch_state_override is not None:
        state = dict(watch_state_override)
    elif persist and workspace_id:
        try:
            state = await watch_db.get_watch_state(workspace_id)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] get_watch_state failed: %s", e)

    emitted: list[ProbeSignal] = []

    if kind == "calendar":
        sigs, state = await probe_calendar(watch_state=state)
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
        emitted.append(
            ProbeSignal(
                probe="schedule",
                signal_key=f"{kind}:{datetime.now(tz).date().isoformat()}",
                priority="P1",
                summary=f"{ticker or name} 计划任务唤醒（{kind}）",
                payload={"kind": kind},
            )
        )
    else:
        # Standard company_watch: run the 5 company probes (agenda, sec, cnhk, earnings, internal) with time-based backoff
        t_sym = ticker or "UNKNOWN"
        eff_ws_id = workspace_id if persist else None
        probe_runners = [
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
                "sec",
                lambda st: probe_sec(
                    ticker=t_sym,
                    watch_state=st,
                    workspace_id=eff_ws_id,
                    allow_network=persist,
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
            (
                "earnings",
                lambda st: probe_earnings(
                    ticker=t_sym,
                    watch_state=st,
                    workspace_id=eff_ws_id,
                    agenda_doc=agenda_doc_override,
                ),
            ),
            (
                "internal",
                lambda st: probe_internal(
                    ticker=t_sym,
                    watch_state=st,
                    workspace_id=eff_ws_id,
                    allow_network=persist,
                ),
            ),
        ]
        for probe_name, runner in probe_runners:
            in_bo, nb_iso = is_in_backoff(state, probe_name)
            if in_bo:
                logger.info(
                    "[DALTON_PREFLIGHT] probe=%s ticker=%s in time-backoff until %s",
                    probe_name,
                    t_sym,
                    nb_iso,
                )
                continue
            try:
                sigs, state = await runner(state)
                if probe_name == "cnhk" and state.get("cnhk_probe_degraded"):
                    state = record_probe_backoff(state, probe_name, "cnhk_probe_degraded")
                else:
                    state = clear_probe_backoff(state, probe_name)
                emitted.extend(sigs)
            except Exception as err:
                logger.warning(
                    "[DALTON_PREFLIGHT] probe=%s ticker=%s transient error: %s",
                    probe_name,
                    t_sym,
                    err,
                )
                state = record_probe_backoff(state, probe_name, str(err))

    # Persist updated watch state (including any auto-resolved CIK or watermarks)
    if persist and workspace_id and ticker:
        try:
            await watch_db.upsert_watch_state(workspace_id, ticker, state)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] upsert_watch_state failed: %s", e)

    # Deduplicate against dalton_signals (idempotent by workspace_id, probe, signal_key; A10)
    candidate_signals: list[dict[str, Any]] = []

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
                candidate_signals.append(sig.to_dict())
    else:
        seen_keys: set[tuple[str, str]] = set(
            tuple(x) for x in (state.get("_woken_signal_keys") or []) if isinstance(x, (list, tuple)) and len(x) == 2
        )
        for sig in emitted:
            pair = (sig.probe, sig.signal_key)
            if pair not in seen_keys:
                candidate_signals.append(sig.to_dict())
                seen_keys.add(pair)
        state["_woken_signal_keys"] = [list(x) for x in sorted(seen_keys)]
        if watch_state_override is not None:
            watch_state_override.clear()
            watch_state_override.update(state)

    if not candidate_signals:
        if persist:
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
        triage_kept, triage_skipped, triage_reason = await _triage_signals_with_llm(
            ticker=ticker,
            workspace_id=workspace_id,
            user_id=user_id,
            signals=triage_candidates,
            agenda_doc=agenda_doc_override,
            watch_state=state,
            persist=persist,
        )
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
        if persist:
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
            woke_signals = [
                s
                for s in woke_signals
                if not s.get("signal_id") or str(s["signal_id"]) in claimed_ids
            ]
            if not woke_signals:
                if persist:
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
            if persist:
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

    if persist:
        try:
            await watch_db.insert_preflight_log(
                automation_id=automation_id,
                automation_execution_id=execution_id,
                workspace_id=workspace_id,
                decision="wake",
                reason=f"{len(woke_signals)}_signals",
                signals=woke_signals,
            )
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] log insert failed: %s", e)

    wake_ctx = format_wake_directive(
        automation_name=name,
        ticker=ticker,
        signals=woke_signals,
    )
    return PreflightResult(
        should_wake=True,
        decision="wake",
        reason=f"{len(woke_signals)}_signals",
        signals=woke_signals,
        wake_context=wake_ctx,
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
    """Run zero-token preflight + optional LLM triage bounded by `preflight_timeout_s` (45s default, A3)."""
    if not is_dalton_automation(automation):
        return PreflightResult(should_wake=True, decision="passthrough", reason="non_dalton")

    settings = get_dalton_settings()
    eff_timeout = float(
        timeout_s
        if timeout_s is not None
        else (settings.autonomy.preflight_timeout_s or 45)
    )

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

    if not settings.autonomy.enabled:
        return PreflightResult(should_wake=True, decision="passthrough", reason="autonomy_disabled")

    try:
        return await asyncio.wait_for(
            _preflight_check_inner(
                automation,
                execution_id,
                verified_ownership,
                watch_state_override=watch_state_override,
                agenda_doc_override=agenda_doc_override,
                persist=persist,
            ),
            timeout=eff_timeout,
        )
    except asyncio.TimeoutError:
        name = str(automation.get("name") or "dalton-task")
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
