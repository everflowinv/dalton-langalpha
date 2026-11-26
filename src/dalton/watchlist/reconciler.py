"""Transactional outbox reconciler for Personal Watchlist tier policies (§5.3)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.dalton.autonomy.schedule import (
    _format_move_pct_label,
    _user_automations_lock_for_write,
    build_company_watch_specs,
    list_all_user_automations,
    schedule_dalton_automation_helper,
)
from src.dalton.autonomy.tracking import (
    _replaced_by_tracker,
    resolve_workspace_id_by_ticker,
    resolve_workspace_tracking_owner,
)
from src.dalton.coverage.models import _ensure_json_obj
from src.dalton.coverage.service import sync_user_coverage_to_home
from src.dalton.database import coverage as coverage_db
from src.dalton.services.company_automations import automation_history_context
from src.dalton.watchlist.policies import (
    TierPolicy,
    get_default_tier_policy,
    get_reconcile_job,
    get_user_tier_policies,
    list_unfinished_reconcile_jobs,
    resolve_effective_company_policy,
    update_reconcile_job_progress,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db

logger = logging.getLogger(__name__)

STANDARD_TASK_SUFFIXES: tuple[str, ...] = ("watch", "delta", "move-up", "move-down")


def _nudge_price_monitor() -> None:
    """Best-effort refresh of PriceMonitorService cache after price trigger changes."""
    try:
        from src.server.services.price_monitor import PriceMonitorService

        PriceMonitorService.get_instance().request_targets_reload()
    except Exception:
        pass


def _is_future_earn_once(row: Mapping[str, Any]) -> bool:
    name = str(row.get("name") or "").strip().lower()
    if "-earn-" not in name:
        return False
    if str(row.get("trigger_type") or "").strip().lower() != "once":
        return False
    status = str(row.get("status") or "").strip().lower()
    if status in ("completed", "disabled", "deleted"):
        return False
    when = row.get("next_run_at")
    if when is None:
        return True
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when > datetime.now(UTC)
    return False


def _has_handoff_pause(row: Mapping[str, Any]) -> bool:
    meta = row.get("metadata")
    if not isinstance(meta, Mapping):
        return False
    reason = meta.get("handoff_paused_reason")
    return bool(str(reason).strip()) if reason is not None else False


def _has_user_pause(row: Mapping[str, Any]) -> bool:
    meta = row.get("metadata")
    if not isinstance(meta, Mapping):
        return False
    return bool(meta.get("paused_by_user"))


def _has_policy_pause(row: Mapping[str, Any]) -> bool:
    meta = row.get("metadata")
    if not isinstance(meta, Mapping):
        return False
    return bool(meta.get("paused_by_policy"))


def _is_replaced_by_tracker(row: Mapping[str, Any]) -> bool:
    if _replaced_by_tracker(dict(row)):
        return True
    meta = row.get("metadata")
    return isinstance(meta, Mapping) and meta.get("replaced_by_tracker") is True


def _company_rows_for_workspace(
    all_rows: Sequence[Mapping[str, Any]],
    *,
    workspace_id: str,
    ticker: str,
) -> list[dict[str, Any]]:
    t_prefix = f"dalton-{ticker.strip().lower()}-"
    ws_clean = str(workspace_id or "").strip()
    matched: list[dict[str, Any]] = []
    for raw in all_rows:
        if not isinstance(raw, Mapping):
            continue
        r_ws = str(raw.get("workspace_id") or "").strip()
        r_name = str(raw.get("name") or "").strip().lower()
        if r_ws == ws_clean and r_name.startswith(t_prefix):
            matched.append(dict(raw))
    return matched


async def resolve_company_workspace_id(
    user_id: str,
    ticker: str,
    *,
    cov_row: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> str | None:
    """Resolve workspace ID for ``(user_id, ticker)`` following §4.2 precedence.

    Order:
    1. Active company coverage unit led by ``user_id``
    2. ``extra._synced_workspace_id``
    3. ``extra._manual_workspace_id`` (or ``extra.workspace.workspace_id``)
    4. User's own non-deleted workspace matching ``ticker``
    """
    uid = str(user_id or "").strip()
    sym = str(ticker or "").strip().upper()
    if not uid or not sym:
        return None

    # 1. Active coverage unit led by user_id
    try:
        from src.dalton.settings import is_coverage_units_enabled

        if is_coverage_units_enabled():
            from src.dalton.coverage.identity import normalize_ticker_key
            from src.dalton.database.coverage_units import list_active_units

            t_key = normalize_ticker_key(sym) or sym
            units = await list_active_units(kind="company", conn=conn)
            for u in units or []:
                lead_uid = str(
                    u.get("lead_analyst_user_id") or u.get("owner_user_id") or ""
                ).strip()
                if lead_uid != uid:
                    continue
                u_ticker = str(u.get("ticker") or "").strip().upper()
                u_key = str(u.get("ticker_key") or "").strip().upper()
                if u_ticker == sym or (u_key and u_key == t_key.upper()):
                    ws_id = str(u.get("workspace_id") or "").strip()
                    if ws_id:
                        return ws_id
    except Exception:
        pass

    # 2 & 3. Row internal extra pointers
    if cov_row is not None:
        raw_extra = _ensure_json_obj(cov_row.get("extra"), {})
        if isinstance(raw_extra, Mapping):
            synced_ws = str(raw_extra.get("_synced_workspace_id") or "").strip()
            if synced_ws:
                return synced_ws
            manual_ws = str(raw_extra.get("_manual_workspace_id") or "").strip()
            if manual_ws:
                return manual_ws
            ws_obj = raw_extra.get("workspace")
            if isinstance(ws_obj, Mapping):
                ws_id = str(ws_obj.get("workspace_id") or "").strip()
                if ws_id:
                    return ws_id

    # 4. User's own workspace by ticker name / knowledge doc
    return await resolve_workspace_id_by_ticker(sym, uid)


def _apply_price_threshold_to_row(
    row: Mapping[str, Any],
    *,
    ticker: str,
    direction: str,
    threshold_pct: float,
) -> dict[str, Any]:
    """Return updated trigger_config, description, and instruction for a price move task."""
    t_upper = ticker.upper()
    pct = float(threshold_pct)
    pct_label = _format_move_pct_label(pct)
    existing_cfg = dict(row.get("trigger_config") or {}) if isinstance(row.get("trigger_config"), Mapping) else {}
    raw_conds = existing_cfg.get("conditions")
    if isinstance(raw_conds, list) and raw_conds and isinstance(raw_conds[0], Mapping):
        cond0 = dict(raw_conds[0])
        cond0["value"] = pct
        new_conds = [cond0, *raw_conds[1:]]
    else:
        cond_type = "pct_change_above" if direction == "up" else "pct_change_below"
        new_conds = [
            {
                "type": cond_type,
                "value": pct,
                "reference": "previous_close",
            }
        ]
    new_cfg = {
        "symbol": t_upper,
        "market": "stock",
        "retrigger": {"mode": "recurring", "cooldown_seconds": 86400},
        **existing_cfg,
        "conditions": new_conds,
    }
    if direction == "up":
        description = f"{t_upper} 单日涨幅 ≥ {pct_label}% 异动归因"
        instruction = (
            f"{t_upper} 日内涨幅达到或超过 +{pct_label}%。请按 `.agents/skills/dalton-stock-move/SKILL.md` "
            "第一步先用 `WebSearch` 与 `x_search` 搜最新新闻和 X（当天与前一天）拿到“今天发生了什么”，"
            "再去查基本面、卖方预期与同行联动原因，并追加 `knowledge_append_verdict`。"
        )
    else:
        description = f"{t_upper} 单日跌幅 ≥ {pct_label}% 异动归因"
        instruction = (
            f"{t_upper} 日内跌幅达到或超过 -{pct_label}%。请按 `.agents/skills/dalton-stock-move/SKILL.md` "
            "第一步先用 `WebSearch` 与 `x_search` 搜最新新闻和 X（当天与前一天）拿到“今天发生了什么”，"
            "再去查基本面、卖方预期与同行联动原因，并追加 `knowledge_append_verdict`。"
        )
    return {
        "trigger_config": new_cfg,
        "description": description,
        "instruction": instruction,
    }


class TierPolicyReconciler:
    """Reconciles personal watchlist outbox jobs per company with deadlock-free locking (§5.3)."""

    async def reconcile_company(
        self,
        user_id: str,
        ticker: str,
        *,
        workspace_id: str | None = None,
        override_user_pause: bool = False,
        target_policy_versions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Reconcile all managed automations for one company under its own ``user_automations_lock``."""
        uid = str(user_id or "").strip()
        sym = str(ticker or "").strip().upper()
        if not uid or not sym:
            return {"ticker": sym, "ok": True, "skipped": True, "reason": "empty_args"}

        pre_row = await coverage_db.get_coverage_row(uid, sym)
        ws_id = workspace_id or await resolve_company_workspace_id(uid, sym, cov_row=pre_row)
        if not ws_id:
            return {
                "ticker": sym,
                "ok": True,
                "skipped": True,
                "reason": "no_workspace",
            }

        price_touched = False
        actions_taken: list[dict[str, Any]] = []
        skipped_executions = 0

        # Stage 2: acquire single-company deadlock-free lock sequence
        # (catalog shared -> coverage_unit FOR SHARE -> lock_user_automations)
        async with _user_automations_lock_for_write(uid, prelock_ws=ws_id) as conn:
            conn_kw = {"conn": conn} if conn is not None else {}

            # Re-verify workspace tracking ownership inside the lock
            owner_uid = await resolve_workspace_tracking_owner(ws_id, conn=conn)
            if owner_uid != uid:
                return {
                    "ticker": sym,
                    "workspace_id": ws_id,
                    "ok": True,
                    "skipped": True,
                    "reason": "owner_changed",
                }

            # Re-read live coverage row and latest tier policies inside the lock
            live_row = await coverage_db.get_coverage_row(uid, sym, conn=conn)
            live_policies = await get_user_tier_policies(uid, conn=conn)

            all_user_autos = await list_all_user_automations(uid, conn=conn)
            company_rows = _company_rows_for_workspace(
                all_user_autos, workspace_id=ws_id, ticker=sym
            )
            by_name: dict[str, dict[str, Any]] = {}
            for r in company_rows:
                r_name = str(r.get("name") or "").strip().lower()
                if r_name and r_name not in by_name:
                    by_name[r_name] = r

            paused_ids: list[str] = []
            t_lower = sym.lower()
            std_names = {
                suffix: f"dalton-{t_lower}-{suffix}" for suffix in STANDARD_TASK_SUFFIXES
            }

            # Case 1: Ticker removed from watchlist (or marked private)
            if live_row is None or bool(live_row.get("is_private")):
                reason_label = "removed" if live_row is None else "private_company"
                with automation_history_context(
                    actor_user_id=uid,
                    action="pause",
                    skip_if_unchanged=True,
                ):
                    for r in company_rows:
                        auto_id = str(r.get("automation_id") or "").strip()
                        r_name = str(r.get("name") or "").strip().lower()
                        if not auto_id:
                            continue
                        is_std = r_name in std_names.values()
                        is_future_earn = (live_row is None) and _is_future_earn_once(r)
                        if not (is_std or is_future_earn):
                            continue
                        new_meta = dict(r.get("metadata") or {}) if isinstance(r.get("metadata"), Mapping) else {}
                        new_meta["paused_by_policy"] = True
                        await auto_db.update_automation(
                            auto_id,
                            uid,
                            status="paused",
                            next_run_at=None,
                            metadata=new_meta,
                            **conn_kw,
                        )
                        paused_ids.append(auto_id)
                        if str(r.get("trigger_type") or "").strip().lower() == "price":
                            price_touched = True
                        actions_taken.append(
                            {"name": r_name, "op": "pause", "reason": reason_label}
                        )

                if paused_ids:
                    skipped_executions = await exec_db.skip_unstarted_executions_for_automations(
                        paused_ids,
                        reason="watchlist_policy",
                        error_message=(
                            "Skipped unstarted execution because ticker was removed from personal watchlist."
                            if live_row is None
                            else "Skipped unstarted execution because company is unlisted/private."
                        ),
                        conn=conn,
                    )
            else:
                # Case 2: Ticker exists & listed -> converge to latest live tier policy + task_overrides
                live_tier = str(live_row.get("tier") or "watch").strip().lower()
                base_policy: TierPolicy = live_policies.get(
                    live_tier, get_default_tier_policy(live_tier)
                )
                # Monotonicity note: always converge to live_policies[live_tier] so an older
                # outbox batch with target_policy_versions[live_tier] < base_policy.policy_version
                # never regresses newer settings.
                if (
                    target_policy_versions
                    and live_tier in target_policy_versions
                    and base_policy.policy_version > int(target_policy_versions[live_tier])
                ):
                    logger.info(
                        "[WATCHLIST_RECONCILE] converging %s to newer policy_version=%s (batch had %s)",
                        sym,
                        base_policy.policy_version,
                        target_policy_versions[live_tier],
                    )
                effective_policy = resolve_effective_company_policy(
                    base_policy, live_row.get("task_overrides")
                )

                desired_specs = build_company_watch_specs(
                    ticker=sym,
                    workspace_id=ws_id,
                    tier=live_tier,
                    policy=effective_policy,
                    is_private=False,
                )
                spec_by_name = {str(s["name"]).lower(): s for s in desired_specs}

                company_paused_by_user = any(
                    _has_user_pause(by_name[n])
                    for n in std_names.values()
                    if n in by_name
                )

                from src.server.services.automation_scheduler import AutomationScheduler

                for suffix, s_name in std_names.items():
                    existing = by_name.get(s_name)
                    spec = spec_by_name.get(s_name)

                    if spec is None:
                        # Task disabled by effective policy -> pause if exists (never create)
                        if existing is None or not existing.get("automation_id"):
                            continue
                        auto_id = str(existing["automation_id"])
                        new_meta = (
                            dict(existing.get("metadata") or {})
                            if isinstance(existing.get("metadata"), Mapping)
                            else {}
                        )
                        new_meta["paused_by_policy"] = True
                        new_meta["tier"] = live_tier
                        with automation_history_context(
                            actor_user_id=uid,
                            action="pause",
                            skip_if_unchanged=True,
                        ):
                            await auto_db.update_automation(
                                auto_id,
                                uid,
                                status="paused",
                                next_run_at=None,
                                metadata=new_meta,
                                **conn_kw,
                            )
                        paused_ids.append(auto_id)
                        if str(existing.get("trigger_type") or "").strip().lower() == "price":
                            price_touched = True
                        actions_taken.append({"name": s_name, "op": "pause"})
                        continue

                    # Task enabled by effective policy
                    if existing is None:
                        if company_paused_by_user and not override_user_pause:
                            actions_taken.append(
                                {"name": s_name, "op": "skip_create", "reason": "paused_by_user"}
                            )
                            continue
                        spec_copy = dict(spec)
                        spec_meta = dict(spec_copy.get("metadata") or {})
                        spec_meta["tier"] = live_tier
                        spec_meta.pop("paused_by_policy", None)
                        spec_meta.pop("paused_by_user", None)
                        spec_copy["metadata"] = spec_meta
                        spec_copy["status"] = "active"
                        with automation_history_context(
                            actor_user_id=uid,
                            action="create",
                            skip_if_unchanged=True,
                        ):
                            await schedule_dalton_automation_helper(
                                s_name,
                                spec_copy,
                                user_id=uid,
                                workspace_id=ws_id,
                                ticker=sym,
                                role="chief_of_staff",
                                action="upsert",
                                conn=conn,
                            )
                        if spec.get("trigger_type") == "price":
                            price_touched = True
                        actions_taken.append({"name": s_name, "op": "create"})
                        continue

                    auto_id = str(existing.get("automation_id") or "").strip()
                    if not auto_id:
                        continue
                    existing_meta = (
                        dict(existing.get("metadata") or {})
                        if isinstance(existing.get("metadata"), Mapping)
                        else {}
                    )
                    new_meta = dict(existing_meta)
                    new_meta["tier"] = live_tier
                    new_meta["ticker"] = sym

                    is_cron = str(spec.get("trigger_type") or "").lower() == "cron"
                    is_price = str(spec.get("trigger_type") or "").lower() == "price"
                    existing_status = str(existing.get("status") or "active").strip().lower()

                    # Pause state matrix (§5.1):
                    # 1. replaced_by_tracker -> keep paused, next_run_at=None, record target cron in metadata only
                    if _is_replaced_by_tracker(existing):
                        if is_cron:
                            new_meta["target_policy_cron"] = spec["cron_expression"]
                            new_meta["target_policy_timezone"] = spec["timezone"]
                        with automation_history_context(
                            actor_user_id=uid,
                            action="update",
                            skip_if_unchanged=True,
                        ):
                            await auto_db.update_automation(
                                auto_id,
                                uid,
                                status="paused",
                                next_run_at=None,
                                metadata=new_meta,
                                **conn_kw,
                            )
                        actions_taken.append(
                            {"name": s_name, "op": "preserve_paused", "reason": "replaced_by_tracker"}
                        )
                        continue

                    # Build trigger update payload (shared by handoff_paused, paused_by_user, and active/resume)
                    trigger_kwargs: dict[str, Any] = {}
                    if is_cron:
                        trigger_kwargs["cron_expression"] = spec["cron_expression"]
                        trigger_kwargs["timezone"] = spec["timezone"]
                    elif is_price:
                        direction = "up" if suffix == "move-up" else "down"
                        trigger_kwargs.update(
                            _apply_price_threshold_to_row(
                                existing,
                                ticker=sym,
                                direction=direction,
                                threshold_pct=effective_policy.move_threshold_pct,
                            )
                        )
                        price_touched = True

                    # 2. handoff_paused_reason -> keep paused, next_run_at=None, sync trigger params
                    if _has_handoff_pause(existing):
                        with automation_history_context(
                            actor_user_id=uid,
                            action="update",
                            skip_if_unchanged=True,
                        ):
                            await auto_db.update_automation(
                                auto_id,
                                uid,
                                status="paused",
                                next_run_at=None,
                                metadata=new_meta,
                                **trigger_kwargs,
                                **conn_kw,
                            )
                        actions_taken.append(
                            {"name": s_name, "op": "preserve_paused", "reason": "handoff_paused_reason"}
                        )
                        continue

                    # 3. paused_by_user (without override_user_pause) -> keep paused, next_run_at=None, sync trigger params
                    if _has_user_pause(existing) and not override_user_pause:
                        new_meta.pop("paused_by_policy", None)
                        with automation_history_context(
                            actor_user_id=uid,
                            action="update",
                            skip_if_unchanged=True,
                        ):
                            await auto_db.update_automation(
                                auto_id,
                                uid,
                                status="paused",
                                next_run_at=None,
                                metadata=new_meta,
                                **trigger_kwargs,
                                **conn_kw,
                            )
                        actions_taken.append(
                            {"name": s_name, "op": "preserve_paused", "reason": "paused_by_user"}
                        )
                        continue

                    # 4. Active, paused_by_policy (no blockers), or override_user_pause -> resume/update active
                    resuming = existing_status == "paused" and (
                        _has_policy_pause(existing) or override_user_pause
                    )
                    if existing_status == "paused" and not resuming:
                        # Paused for an unknown/manual reason without paused_by_policy and without override:
                        # keep paused with next_run_at=None while syncing params.
                        with automation_history_context(
                            actor_user_id=uid,
                            action="update",
                            skip_if_unchanged=True,
                        ):
                            await auto_db.update_automation(
                                auto_id,
                                uid,
                                status="paused",
                                next_run_at=None,
                                metadata=new_meta,
                                **trigger_kwargs,
                                **conn_kw,
                            )
                        actions_taken.append(
                            {"name": s_name, "op": "preserve_paused", "reason": "paused_external"}
                        )
                        continue

                    new_meta.pop("paused_by_policy", None)
                    if override_user_pause:
                        new_meta.pop("paused_by_user", None)
                    next_run_at = (
                        AutomationScheduler.calculate_first_run(
                            spec["cron_expression"], spec["timezone"]
                        )
                        if is_cron
                        else None
                    )
                    with automation_history_context(
                        actor_user_id=uid,
                        action="resume" if resuming else "update",
                        skip_if_unchanged=True,
                    ):
                        await auto_db.update_automation(
                            auto_id,
                            uid,
                            status="active",
                            next_run_at=next_run_at,
                            metadata=new_meta,
                            **trigger_kwargs,
                            **conn_kw,
                        )
                    actions_taken.append(
                        {"name": s_name, "op": "resume" if resuming else "update"}
                    )

                if paused_ids:
                    skipped_executions = await exec_db.skip_unstarted_executions_for_automations(
                        paused_ids,
                        reason="watchlist_policy",
                        error_message="Skipped unstarted execution because tier policy paused task.",
                        conn=conn,
                    )

        if price_touched:
            _nudge_price_monitor()

        return {
            "ticker": sym,
            "workspace_id": ws_id,
            "ok": True,
            "actions": actions_taken,
            "skipped_executions": skipped_executions,
        }

    async def reconcile_job(
        self,
        user_id: str,
        job_id: str,
        *,
        sync_home: bool = True,
    ) -> dict[str, Any]:
        """Execute Stage 2 (per-company transactions) + Stage 3 (Home YAML sync) for one outbox job."""
        uid = str(user_id or "").strip()
        jid = str(job_id or "").strip()
        job = await get_reconcile_job(uid, jid)
        if job is None:
            return {"ok": False, "error": "job_not_found", "job_id": jid}

        await update_reconcile_job_progress(uid, jid, status="running")

        target_versions = dict(job.get("target_policy_versions") or {})
        override_user_pause = bool(job.get("override_user_pause", False))
        completed: list[str] = list(job.get("completed_tickers") or [])
        completed_set = set(completed)

        # Retry both pending_tickers and previously failed tickers (idempotent recovery)
        retry_tickers: list[str] = []
        for t in job.get("pending_tickers") or []:
            sym = str(t or "").strip().upper()
            if sym and sym not in completed_set and sym not in retry_tickers:
                retry_tickers.append(sym)
        for item in job.get("failed_items") or []:
            if isinstance(item, Mapping):
                sym = str(item.get("ticker") or "").strip().upper()
                if sym and sym not in completed_set and sym not in retry_tickers:
                    retry_tickers.append(sym)

        remaining_pending = list(retry_tickers)
        new_failed: list[dict[str, Any]] = []
        company_results: list[dict[str, Any]] = []

        for sym in retry_tickers:
            try:
                res = await self.reconcile_company(
                    uid,
                    sym,
                    override_user_pause=override_user_pause,
                    target_policy_versions=target_versions,
                )
                company_results.append(res)
                completed.append(sym)
                completed_set.add(sym)
                remaining_pending = [x for x in remaining_pending if x != sym]
                await update_reconcile_job_progress(
                    uid,
                    jid,
                    pending_tickers=remaining_pending,
                    completed_tickers=completed,
                    failed_items=new_failed,
                )
            except Exception as exc:
                logger.warning(
                    "[WATCHLIST_RECONCILE] per-company reconcile failed | user=%s job=%s ticker=%s exc_type=%s",
                    uid,
                    jid,
                    sym,
                    type(exc).__name__,
                )
                new_failed.append(
                    {
                        "ticker": sym,
                        "error_code": "reconcile_company_failed",
                        "exc_type": type(exc).__name__,
                    }
                )
                remaining_pending = [x for x in remaining_pending if x != sym]
                await update_reconcile_job_progress(
                    uid,
                    jid,
                    pending_tickers=remaining_pending,
                    completed_tickers=completed,
                    failed_items=new_failed,
                    status="partial",
                )

        # Stage 3: Home YAML sync
        yaml_synced = bool(job.get("yaml_synced", False))
        if sync_home and not yaml_synced:
            synced = bool(await sync_user_coverage_to_home(uid))
            if synced:
                yaml_synced = True

        final_status = (
            "completed"
            if not remaining_pending and not new_failed and yaml_synced
            else "partial"
        )
        updated_job = await update_reconcile_job_progress(
            uid,
            jid,
            pending_tickers=remaining_pending,
            completed_tickers=completed,
            failed_items=new_failed,
            yaml_synced=yaml_synced,
            status=final_status,
        )
        return {
            "ok": final_status == "completed",
            "job_id": jid,
            "status": final_status,
            "yaml_synced": yaml_synced,
            "completed_tickers": completed,
            "pending_tickers": remaining_pending,
            "failed_items": new_failed,
            "company_results": company_results,
            "job": updated_job,
        }

    async def reconcile_pending_jobs(
        self,
        user_id: str,
        *,
        sync_home: bool = True,
    ) -> dict[str, Any]:
        """Idempotently drain all unfinished or YAML-unsynced outbox jobs for ``user_id``."""
        uid = str(user_id or "").strip()
        if not uid:
            return {"ok": True, "processed_jobs": 0, "results": []}
        jobs = await list_unfinished_reconcile_jobs(uid)
        results: list[dict[str, Any]] = []
        for job in jobs:
            res = await self.reconcile_job(uid, str(job["job_id"]), sync_home=sync_home)
            results.append(res)
        all_done = all(r.get("status") == "completed" for r in results)
        return {
            "ok": all_done,
            "processed_jobs": len(results),
            "results": results,
        }

    async def mark_yaml_synced_for_user(self, user_id: str) -> int:
        """Mark unsynced outbox jobs as ``yaml_synced=TRUE`` after an external Home sync succeeded."""
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        jobs = await list_unfinished_reconcile_jobs(uid)
        updated = 0
        for job in jobs:
            if job.get("yaml_synced"):
                continue
            pending = list(job.get("pending_tickers") or [])
            failed = list(job.get("failed_items") or [])
            next_status = "completed" if (not pending and not failed) else str(job.get("status") or "partial")
            await update_reconcile_job_progress(
                uid,
                str(job["job_id"]),
                yaml_synced=True,
                status=next_status,
            )
            updated += 1
        return updated


__all__ = [
    "STANDARD_TASK_SUFFIXES",
    "TierPolicyReconciler",
    "resolve_company_workspace_id",
]
