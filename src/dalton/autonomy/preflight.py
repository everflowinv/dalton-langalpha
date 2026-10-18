"""Zero-token preflight engine for Dalton autonomous tasks (PK.16).

Runs before `transition_execution(..., to="running")` and before sandbox wakeup
in `AutomationExecutor.execute()`. When no actionable signal is found, the
execution settles immediately as `Outcome.PRECHECK_SKIPPED` (`status='skipped'`,
`skip_reason='precheck'`) with zero LLM tokens and zero sandbox startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.dalton.autonomy.backoff import (
    clear_probe_backoff,
    is_in_backoff,
    record_probe_backoff,
)
from src.dalton.autonomy.probes import ProbeSignal
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.calendar import probe_calendar
from src.dalton.autonomy.probes.consensus import probe_consensus
from src.dalton.autonomy.probes.director import probe_director
from src.dalton.autonomy.probes.earnings import probe_earnings
from src.dalton.autonomy.probes.internal import probe_internal
from src.dalton.autonomy.probes.price import probe_price
from src.dalton.autonomy.probes.sec import probe_sec
from src.dalton.database import watch as watch_db
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)


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
    lines = [
        f'<dalton_wake automation="{automation_name}"{ticker_attr} checked_at="{ts}" signal_count="{len(signals)}">',
        "以下是由零 token 预检探针检测到的真实变更信号，请优先围绕这些信号开展取证与分析，并在结束前调用 `knowledge_append_verdict` 记录五词结论：",
    ]
    for idx, sig in enumerate(signals, start=1):
        probe = sig.get("probe") or "probe"
        prio = sig.get("priority") or "P1"
        key = sig.get("signal_key") or ""
        summary = sig.get("summary") or ""
        lines.append(f"  {idx}. [{prio}][{probe}] {summary} (key={key})")
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


async def _check_budget_available() -> tuple[bool, Optional[str]]:
    """Return (False, 'budget_exhausted') if the shared daily USD budget is exhausted."""
    try:
        from src.dalton.budget.service import get_budget_today

        snap = await get_budget_today()
        cap = float(snap.get("daily_cap_usd") or 0.0)
        spent = float(snap.get("spent_today_usd") or 0.0)
        reserved = float(snap.get("reserved_usd") or 0.0)
        if cap > 0 and (spent + reserved) >= cap:
            return False, "budget_exhausted"
    except Exception as e:
        logger.debug("[DALTON_PREFLIGHT] budget check skipped: %s", e)
    return True, None


async def preflight_check(
    automation: dict[str, Any],
    execution_id: str,
    *,
    watch_state_override: Optional[dict[str, Any]] = None,
    agenda_doc_override: Optional[dict[str, Any]] = None,
    persist: bool = True,
) -> PreflightResult:
    """Run zero-token preflight for a Dalton automation firing (PK.16)."""
    if not get_dalton_settings().autonomy.enabled:
        return PreflightResult(should_wake=True, decision="passthrough", reason="autonomy_disabled")

    if not is_dalton_automation(automation):
        return PreflightResult(should_wake=True, decision="passthrough", reason="non_dalton")

    automation_id = str(automation.get("automation_id") or "") or None
    workspace_id = str(automation["workspace_id"]) if automation.get("workspace_id") else None
    name = str(automation.get("name") or "dalton-task")
    ticker = _extract_ticker(automation)
    kind = _determine_kind(automation)

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

    # Check shared daily USD cap before running probes or waking sandbox
    budget_ok, budget_reason = await _check_budget_available()
    if not budget_ok:
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
        sigs, state = await probe_director(kind=kind, watch_state=state)
        emitted.extend(sigs)
    elif kind in ("weekly_delta", "earnings_window", "custom_dalton"):
        emitted.append(
            ProbeSignal(
                probe="schedule",
                signal_key=f"{kind}:{datetime.now(timezone.utc).date().isoformat()}",
                priority="P1",
                summary=f"{ticker or name} 计划任务唤醒（{kind}）",
                payload={"kind": kind},
            )
        )
    else:
        # Standard company_watch: run all 6 company probes with time-based backoff protection
        t_sym = ticker or "UNKNOWN"
        probe_runners = [
            (
                "agenda",
                lambda st: probe_agenda(
                    workspace_id=workspace_id,
                    ticker=t_sym,
                    watch_state=st,
                    agenda_doc=agenda_doc_override,
                ),
            ),
            ("sec", lambda st: probe_sec(ticker=t_sym, watch_state=st)),
            ("earnings", lambda st: probe_earnings(ticker=t_sym, watch_state=st)),
            ("price", lambda st: probe_price(ticker=t_sym, watch_state=st)),
            ("consensus", lambda st: probe_consensus(ticker=t_sym, watch_state=st)),
            ("internal", lambda st: probe_internal(ticker=t_sym, watch_state=st)),
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

    # Persist updated watch state
    if persist and workspace_id and ticker:
        try:
            await watch_db.upsert_watch_state(workspace_id, ticker, state)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] upsert_watch_state failed: %s", e)

    # Deduplicate against dalton_signals (idempotent by workspace_id, probe, signal_key)
    woke_signals: list[dict[str, Any]] = []
    signal_ids_to_mark: list[str] = []

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
                    woke_signals.append(
                        {
                            "signal_id": str(row.get("signal_id") or ""),
                            "probe": sig.probe,
                            "signal_key": sig.signal_key,
                            "priority": sig.priority,
                            "summary": sig.summary,
                            "payload": sig.payload,
                        }
                    )
                    if row.get("signal_id"):
                        signal_ids_to_mark.append(str(row["signal_id"]))
            except Exception as e:
                logger.warning("[DALTON_PREFLIGHT] upsert_signal failed: %s", e)
                woke_signals.append(sig.to_dict())
    else:
        seen_keys: set[tuple[str, str]] = set(
            tuple(x) for x in (state.get("_woken_signal_keys") or []) if isinstance(x, (list, tuple)) and len(x) == 2
        )
        for sig in emitted:
            pair = (sig.probe, sig.signal_key)
            if pair not in seen_keys:
                woke_signals.append(sig.to_dict())
                seen_keys.add(pair)
        state["_woken_signal_keys"] = [list(x) for x in sorted(seen_keys)]
        if watch_state_override is not None:
            watch_state_override.clear()
            watch_state_override.update(state)

    if not woke_signals:
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

    if persist and signal_ids_to_mark:
        try:
            await watch_db.mark_signals_woken(signal_ids_to_mark, execution_id)
        except Exception as e:
            logger.warning("[DALTON_PREFLIGHT] mark_signals_woken failed: %s", e)

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
        res = await preflight_check(automation, execution_id)
        if res.decision == "passthrough":
            return None
        return res
    except Exception as e:
        logger.warning("[DALTON_PREFLIGHT] decide() unexpected error (waking fallback): %s", e)
        return None
