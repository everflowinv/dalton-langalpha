"""Transactional outbox reconciler for Personal Watchlist tier policies (§5.3)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
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
from src.dalton.watchlist.pause_guards import (
    build_locked_resume_reconciler,
    can_override_user_pause_locked,
    is_earnings_task_allowed_for_stages,
    parse_earnings_task_name,
)
from src.dalton.watchlist.policies import (
    TierPolicy,
    claim_reconcile_job,
    claim_unfinished_reconcile_jobs_for_startup,
    get_default_tier_policy,
    get_reconcile_job,
    get_user_tier_policies,
    list_unfinished_reconcile_jobs,
    renew_reconcile_job_lease,
    resolve_effective_company_policy,
    update_reconcile_job_progress,
)
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database import workspace as ws_db

logger = logging.getLogger(__name__)

STANDARD_TASK_SUFFIXES: tuple[str, ...] = ("watch", "delta", "move-up", "move-down")
AUTOMATIONS_RELOAD_SIGNAL = "__automations__"
_PURE_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,11}$")


async def _nudge_price_monitor() -> None:
    """Refresh PriceMonitorService price automation cache locally and cross-process via Redis."""
    try:
        from src.server.services.price_monitor import PriceMonitorService

        PriceMonitorService.get_instance().request_automations_reload()
    except Exception:
        pass
    try:
        from src.server.services.market_targets.signals import TARGETS_CHANGED_CHANNEL
        from src.server.services.workspace_status_pubsub import get_shared_pubsub_client

        client = await get_shared_pubsub_client()
        if client is not None:
            await client.publish(TARGETS_CHANGED_CHANNEL, AUTOMATIONS_RELOAD_SIGNAL)
    except Exception:
        pass


def _ticker_slugs(ticker: str) -> tuple[str, ...]:
    """Return canonical lowercase ticker slugs covering both dot and hyphen forms (e.g. 0700.hk / 0700-hk)."""
    t_lower = str(ticker or "").strip().lower()
    if not t_lower:
        return ()
    hyphen = t_lower.replace(".", "-")
    if hyphen != t_lower:
        return (t_lower, hyphen)
    return (t_lower,)


def _find_standard_task_row(
    company_rows: Sequence[Mapping[str, Any]],
    ticker: str,
    suffix: str,
) -> dict[str, Any] | None:
    """Find a standard managed task row matching any slug of ``ticker`` for ``suffix``."""
    candidates = tuple(f"dalton-{slug}-{suffix}" for slug in _ticker_slugs(ticker))
    by_name: dict[str, dict[str, Any]] = {}
    for r in company_rows:
        if not isinstance(r, Mapping):
            continue
        r_name = str(r.get("name") or "").strip().lower()
        if r_name and r_name not in by_name:
            by_name[r_name] = dict(r)
    for cand in candidates:
        if cand in by_name:
            return by_name[cand]
    return None


def _earn_stage_from_name(name: str, ticker: str) -> str | None:
    """Extract earnings task kind (``tm3`` | ``t0`` | ``tp1`` | ``recheck``) via shared classifier."""
    parsed = parse_earnings_task_name(name)
    if parsed is None:
        return None
    slug, kind = parsed
    if slug not in _ticker_slugs(ticker):
        return None
    return kind


async def is_unstarted_earn_once(
    row: Mapping[str, Any],
    *,
    user_id: str | None = None,
    conn: Any = None,
) -> bool:
    """Return True iff ``row`` is an unstarted one-shot earnings task (``tm3``/``t0``/``tp1``/``recheck``).

    Includes tasks already claimed by the scheduler (``last_run_at`` stamped on claim)
    whose execution rows are still ``pending`` / ``waiting`` and have never started running.
    Wraps both primary and fallback queries in savepoints when running inside a transaction.
    Fails safe (returns True -> pause) if execution status lookup fails.
    """
    name = str(row.get("name") or "").strip().lower()
    if parse_earnings_task_name(name) is None:
        return False
    if str(row.get("trigger_type") or "").strip().lower() != "once":
        return False
    status = str(row.get("status") or "").strip().lower()
    if status in ("completed", "disabled", "deleted"):
        return False
    run_count = row.get("run_count")
    if isinstance(run_count, int) and run_count > 0:
        return False
    if row.get("last_run_at") is None:
        return True

    # Scheduler sets last_run_at when inserting a 'pending' execution before the run begins.
    # Inspect execution statuses to catch claimed-but-unstarted rows.
    auto_id = str(row.get("automation_id") or "").strip()
    if not auto_id:
        return False
    uid = str(user_id or row.get("user_id") or "").strip()
    statuses: list[str] = []
    queried_ok = False
    sql_conn = conn if (conn is not None and callable(getattr(conn, "cursor", None))) else None
    if sql_conn is not None:
        try:
            tx_fn = getattr(sql_conn, "transaction", None)
            if callable(tx_fn):
                async with tx_fn():
                    async with sql_conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT status
                            FROM automation_executions
                            WHERE automation_id = %s::uuid
                            ORDER BY created_at DESC
                            LIMIT 10
                            """,
                            (auto_id,),
                        )
                        exec_rows = await cur.fetchall()
            else:
                async with sql_conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT status
                        FROM automation_executions
                        WHERE automation_id = %s::uuid
                        ORDER BY created_at DESC
                        LIMIT 10
                        """,
                        (auto_id,),
                    )
                    exec_rows = await cur.fetchall()
            queried_ok = True
            for er in exec_rows or []:
                st = er["status"] if isinstance(er, Mapping) else er[0]
                if st is not None:
                    statuses.append(str(st).strip().lower())
        except Exception:
            queried_ok = False
            statuses = []
    if not queried_ok:
        if not uid:
            # Fail-safe: unable to query executions without user_id -> pause
            return True
        try:
            if sql_conn is not None:
                tx_fn = getattr(sql_conn, "transaction", None)
                if callable(tx_fn):
                    async with tx_fn():
                        exec_res = await exec_db.list_executions(
                            uid, automation_id=auto_id, limit=10, conn=sql_conn
                        )
                else:
                    exec_res = await exec_db.list_executions(
                        uid, automation_id=auto_id, limit=10, conn=sql_conn
                    )
            else:
                exec_res = await exec_db.list_executions(
                    uid, automation_id=auto_id, limit=10
                )
            exec_list = exec_res[0] if isinstance(exec_res, tuple) else exec_res
            queried_ok = True
            for er in exec_list or []:
                if isinstance(er, Mapping) and er.get("status") is not None:
                    statuses.append(str(er["status"]).strip().lower())
        except Exception:
            # Fail-safe: query failure -> treat as should-pause
            return True
    if not statuses:
        return False
    has_unstarted = any(s in ("pending", "waiting") for s in statuses)
    has_started = any(s in ("running", "completed", "failed", "timed_out") for s in statuses)
    return has_unstarted and not has_started


def _is_future_earn_once(row: Mapping[str, Any]) -> bool:
    """Synchronous fast-path check for unstarted one-shot earnings tasks."""
    name = str(row.get("name") or "").strip().lower()
    if parse_earnings_task_name(name) is None:
        return False
    if str(row.get("trigger_type") or "").strip().lower() != "once":
        return False
    status = str(row.get("status") or "").strip().lower()
    if status in ("completed", "disabled", "deleted"):
        return False
    if row.get("last_run_at") is not None:
        return False
    run_count = row.get("run_count")
    if isinstance(run_count, int) and run_count > 0:
        return False
    return True


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


def _can_override_user_pause(
    existing: Mapping[str, Any],
    *,
    override_user_pause: bool,
    override_pause_versions: Mapping[str, int] | None,
) -> bool:
    """Return True iff ``override_user_pause`` is authorized for ``existing``'s current ``config_version``."""
    return can_override_user_pause_locked(
        existing,
        override_user_pause=override_user_pause,
        override_pause_versions=override_pause_versions,
    )


def _company_rows_for_workspace(
    all_rows: Sequence[Mapping[str, Any]],
    *,
    workspace_id: str,
    ticker: str,
) -> list[dict[str, Any]]:
    prefixes = tuple(f"dalton-{slug}-" for slug in _ticker_slugs(ticker))
    ws_clean = str(workspace_id or "").strip()
    if not prefixes or not ws_clean:
        return []
    matched: list[dict[str, Any]] = []
    for raw in all_rows:
        if not isinstance(raw, Mapping):
            continue
        r_ws = str(raw.get("workspace_id") or "").strip()
        r_name = str(raw.get("name") or "").strip().lower()
        if r_ws == ws_clean and any(r_name.startswith(p) for p in prefixes):
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
    1. Active company coverage unit led by ``user_id`` (via ``list_active_company_units_with_ticker``)
    2. ``extra._synced_workspace_id``
    3. ``extra._manual_workspace_id`` (or ``extra.workspace.workspace_id``)
    4. User's own non-deleted workspace matching ``ticker``
    """
    uid = str(user_id or "").strip()
    sym = str(ticker or "").strip().upper()
    if not uid or not sym:
        return None

    # 1. Active coverage unit led by user_id (Item 16: capability-gated, real query, no swallowed DB errors)
    from src.dalton.capabilities import CapabilityId, has_capability
    from src.dalton.settings import is_coverage_units_enabled

    if has_capability(CapabilityId.COLLEAGUE_SHARING) and is_coverage_units_enabled():
        from src.dalton.coverage.identity import normalize_ticker_key
        from src.dalton.database import coverage_units as cu_db

        t_key = normalize_ticker_key(sym) or sym
        units = await cu_db.list_active_company_units_with_ticker(conn=conn)
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


async def _verify_workspace_bound_to_ticker(
    user_id: str,
    workspace_id: str,
    ticker: str,
    *,
    conn: Any = None,
) -> bool:
    """Positively verify inside the write lock that ``workspace_id`` belongs to ``ticker`` (not rebound)."""
    sym = str(ticker or "").strip().upper()
    ws_clean = str(workspace_id or "").strip()
    if not sym or not ws_clean:
        return False

    # 0. Reject immediately if ANY other live coverage row for user_id has rebound this workspace_id
    try:
        live_rows = await coverage_db.list_coverage_rows(user_id, conn=conn)
    except RuntimeError as exc:
        if "database pool is not open" not in str(exc):
            raise
        live_rows = []
    for row in live_rows:
        r_sym = str(row.get("ticker") or "").strip().upper()
        if not r_sym or r_sym == sym:
            continue
        extra = _ensure_json_obj(row.get("extra"), {})
        if not isinstance(extra, Mapping):
            continue
        bound_ws = (
            str(extra.get("_synced_workspace_id") or "").strip()
            or str(extra.get("_manual_workspace_id") or "").strip()
            or (
                str((extra.get("workspace") or {}).get("workspace_id") or "").strip()
                if isinstance(extra.get("workspace"), Mapping)
                else ""
            )
        )
        if bound_ws == ws_clean:
            return False

    from src.dalton.capabilities import CapabilityId, has_capability
    from src.dalton.coverage.identity import canonical_workspace_ticker_key, normalize_ticker_key
    from src.dalton.settings import is_coverage_units_enabled

    t_key = (normalize_ticker_key(sym) or sym).upper()

    # 1. Active coverage unit check (authoritative when present)
    if has_capability(CapabilityId.COLLEAGUE_SHARING) and is_coverage_units_enabled():
        from src.dalton.database import coverage_units as cu_db

        unit = await cu_db.get_active_unit_by_workspace(ws_clean, conn=conn)
        if unit is not None:
            u_ticker = str(unit.get("ticker") or "").strip().upper()
            u_key = str(unit.get("ticker_key") or "").strip().upper()
            if u_ticker or u_key:
                return u_ticker == sym or (bool(u_key) and u_key == t_key)

    # 2. Workspace row existence, non-deleted, and positive company identity match
    try:
        ws_row = await ws_db.get_workspace(ws_clean, conn=conn)
    except RuntimeError as exc:
        if "database pool is not open" not in str(exc):
            raise
        ws_row = None

    if ws_row is None:
        return False
    if str(ws_row.get("status") or "").strip().lower() == "deleted":
        return False

    ws_name = str(ws_row.get("name") or "").strip()
    ws_name_upper = ws_name.upper()
    if ws_name_upper == sym:
        return True
    ws_canon = canonical_workspace_ticker_key(ws_name)
    sym_canon = canonical_workspace_ticker_key(sym)
    if ws_canon and sym_canon and ws_canon == sym_canon:
        return True
    if ws_name_upper and _PURE_TICKER_RE.fullmatch(ws_name_upper) and ws_name_upper != sym:
        return False

    # 3. Positive fallback: read the CURRENT active company identity/document inside ws_clean itself
    # (never search historical knowledge versions across workspaces).
    # Pass current conn & raise_on_db_error=True so doc/workspace DB failures propagate out of the
    # savepoint rather than collapsing into ValueError; only return False when a DIFFERENT ticker
    # is positively resolved. DB failures or unresolved custom workspaces fail-safe as still bound.
    try:
        from src.dalton.autonomy.tracking import _resolve_workspace_ticker

        tx_fn = getattr(conn, "transaction", None) if conn is not None else None
        if callable(tx_fn):
            async with tx_fn():
                cur_ws_ticker = await _resolve_workspace_ticker(
                    ws_clean, conn=conn, raise_on_db_error=True
                )
        else:
            cur_ws_ticker = await _resolve_workspace_ticker(
                ws_clean, conn=conn, raise_on_db_error=True
            )
    except ValueError:
        # Unable to positively identify a different ticker -> fail-safe as still bound
        return True
    except Exception as exc:
        logger.warning(
            "[WATCHLIST_RECONCILE] workspace identity lookup failed for ws=%s ticker=%s (%s); treating as still bound",
            ws_clean,
            sym,
            type(exc).__name__,
        )
        return True
    if cur_ws_ticker:
        cur_canon = canonical_workspace_ticker_key(str(cur_ws_ticker))
        if cur_canon and sym_canon and cur_canon == sym_canon:
            return True
        if str(cur_ws_ticker).strip().upper() == sym:
            return True
        # Positively resolved a DIFFERENT ticker -> workspace was rebound
        return False
    return True


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
        removed_workspace_id: str | None = None,
        override_user_pause: bool = False,
        override_pause_versions: Mapping[str, int] | None = None,
        target_policy_versions: Mapping[str, int] | None = None,
        job_id: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile all managed automations for one company under its own ``user_automations_lock``."""
        uid = str(user_id or "").strip()
        sym = str(ticker or "").strip().upper()
        if not uid or not sym:
            return {"ticker": sym, "ok": True, "skipped": True, "reason": "empty_args"}

        pre_row = await coverage_db.get_coverage_row(uid, sym)
        if pre_row is None:
            # Decision 5: remove compensation strictly uses the workspace_id captured in the Outbox.
            ws_id = workspace_id or removed_workspace_id
        else:
            ws_id = (
                workspace_id
                or await resolve_company_workspace_id(uid, sym, cov_row=pre_row)
                or removed_workspace_id
            )
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

            # r6: fence DB side effects with lease verification INSIDE the automation lock & same transaction
            if job_id and claim_token:
                lease_still_held = await renew_reconcile_job_lease(
                    uid, job_id, claim_token=claim_token, conn=conn
                )
                if not lease_still_held:
                    return {
                        "ticker": sym,
                        "workspace_id": ws_id,
                        "ok": False,
                        "lease_lost": True,
                        "reason": "lease_lost",
                        "actions": [],
                        "skipped_executions": 0,
                    }

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
            if live_row is None:
                # Item 6 / r6: verify workspace is still bound to this ticker before pausing on remove;
                # query errors fail safe (still_bound=True) so removed-ticker tasks are paused, never skipped.
                try:
                    tx_fn = getattr(conn, "transaction", None) if conn is not None else None
                    if callable(tx_fn):
                        async with tx_fn():
                            still_bound = await _verify_workspace_bound_to_ticker(
                                uid, ws_id, sym, conn=conn
                            )
                    else:
                        still_bound = await _verify_workspace_bound_to_ticker(
                            uid, ws_id, sym, conn=conn
                        )
                except Exception as exc:
                    logger.warning(
                        "[WATCHLIST_RECONCILE] _verify_workspace_bound_to_ticker failed for ws=%s ticker=%s (%s); treating as still bound",
                        ws_id,
                        sym,
                        type(exc).__name__,
                    )
                    still_bound = True
                if not still_bound:
                    return {
                        "ticker": sym,
                        "workspace_id": ws_id,
                        "ok": True,
                        "skipped": True,
                        "reason": "workspace_ticker_changed",
                    }

            live_policies = await get_user_tier_policies(uid, conn=conn)

            all_user_autos = await list_all_user_automations(uid, conn=conn)
            company_rows = _company_rows_for_workspace(
                all_user_autos, workspace_id=ws_id, ticker=sym
            )

            paused_ids: list[str] = []
            t_lower = sym.lower()
            std_names = {
                suffix: f"dalton-{t_lower}-{suffix}" for suffix in STANDARD_TASK_SUFFIXES
            }
            all_std_names = {
                f"dalton-{slug}-{suffix}"
                for slug in _ticker_slugs(sym)
                for suffix in STANDARD_TASK_SUFFIXES
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
                        r_status = str(r.get("status") or "active").strip().lower()
                        if not auto_id or r_status == "disabled":
                            continue
                        is_std = r_name in all_std_names
                        is_future_earn = await is_unstarted_earn_once(
                            r, user_id=uid, conn=conn
                        )
                        if not (is_std or is_future_earn):
                            continue
                        new_meta = (
                            dict(r.get("metadata") or {})
                            if isinstance(r.get("metadata"), Mapping)
                            else {}
                        )
                        # Preserve manual/external pause: only mark paused_by_policy if row was active or already policy-paused
                        if r_status == "active" or _has_policy_pause(r):
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

                # Respect any un-overridden user pause on any standard task for new task creation
                company_has_active_user_pause = any(
                    _has_user_pause(row)
                    and not _can_override_user_pause(
                        row,
                        override_user_pause=override_user_pause,
                        override_pause_versions=override_pause_versions,
                    )
                    for suffix in STANDARD_TASK_SUFFIXES
                    if (row := _find_standard_task_row(company_rows, sym, suffix)) is not None
                )

                from src.server.services.automation_scheduler import AutomationScheduler

                for suffix, s_name in std_names.items():
                    existing = _find_standard_task_row(company_rows, sym, suffix)
                    spec = spec_by_name.get(s_name)

                    if spec is None:
                        # Task disabled by effective policy -> pause if exists (never create)
                        if existing is None or not existing.get("automation_id"):
                            continue
                        existing_status = str(existing.get("status") or "active").strip().lower()
                        if existing_status == "disabled":
                            actions_taken.append({"name": s_name, "op": "preserve_disabled"})
                            continue
                        auto_id = str(existing["automation_id"])
                        new_meta = (
                            dict(existing.get("metadata") or {})
                            if isinstance(existing.get("metadata"), Mapping)
                            else {}
                        )
                        # Preserve manual/external pause: do NOT stamp paused_by_policy onto an already manually paused row
                        if existing_status == "active" or _has_policy_pause(existing):
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
                        actions_taken.append({"name": str(existing.get("name") or s_name).lower(), "op": "pause"})
                        continue

                    # Task enabled by effective policy
                    if existing is None:
                        if company_has_active_user_pause:
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
                            sched_res = await schedule_dalton_automation_helper(
                                s_name,
                                spec_copy,
                                user_id=uid,
                                workspace_id=ws_id,
                                ticker=sym,
                                role="chief_of_staff",
                                action="upsert",
                                conn=conn,
                            )
                        # Item 7: treat ok=False from schedule helper as reconcile failure
                        if not isinstance(sched_res, Mapping) or not sched_res.get("ok"):
                            err_code = (
                                str(sched_res.get("error") or "schedule_failed")
                                if isinstance(sched_res, Mapping)
                                else "schedule_failed"
                            )
                            raise RuntimeError(
                                f"schedule_dalton_automation_helper failed for {s_name}: {err_code}"
                            )
                        if spec.get("trigger_type") == "price":
                            price_touched = True
                        actions_taken.append({"name": s_name, "op": "create"})
                        continue

                    auto_id = str(existing.get("automation_id") or "").strip()
                    if not auto_id:
                        continue
                    existing_status = str(existing.get("status") or "active").strip().lower()
                    # Item 8: never transition disabled back to active
                    if existing_status == "disabled":
                        actions_taken.append({"name": str(existing.get("name") or s_name).lower(), "op": "preserve_disabled"})
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
                    eff_override = _can_override_user_pause(
                        existing,
                        override_user_pause=override_user_pause,
                        override_pause_versions=override_pause_versions,
                    )

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

                    # 3. paused_by_user (without valid version-bound override) -> keep paused, next_run_at=None
                    if _has_user_pause(existing) and not eff_override:
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

                    # 4. Active, paused_by_policy (no blockers), or authorized override -> resume/update active
                    resuming = existing_status == "paused" and (
                        _has_policy_pause(existing) or eff_override
                    )
                    if existing_status == "paused" and not resuming:
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
                    if eff_override:
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
                        locked_row_reconciler=build_locked_resume_reconciler(
                            override_user_pause=override_user_pause,
                            override_pause_versions=override_pause_versions,
                        ),
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

                # Item 9 / r5 / r7: pause unstarted earnings tasks (tm3/t0/tp1/recheck) using shared rules
                allowed_earn_stages = set(effective_policy.earnings_stages)
                for r in company_rows:
                    if not (await is_unstarted_earn_once(r, user_id=uid, conn=conn)):
                        continue
                    auto_id = str(r.get("automation_id") or "").strip()
                    r_name = str(r.get("name") or "").strip().lower()
                    if not auto_id:
                        continue
                    earn_kind = _earn_stage_from_name(r_name, sym)
                    if not is_earnings_task_allowed_for_stages(earn_kind, allowed_earn_stages):
                        r_status_earn = str(r.get("status") or "active").strip().lower()
                        new_meta = (
                            dict(r.get("metadata") or {})
                            if isinstance(r.get("metadata"), Mapping)
                            else {}
                        )
                        # Preserve manual/external pause: only mark paused_by_policy if active or already policy-paused
                        if r_status_earn == "active" or _has_policy_pause(r):
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
                        actions_taken.append(
                            {"name": r_name, "op": "pause", "reason": "earnings_stage_disabled"}
                        )

                if paused_ids:
                    skipped_executions = await exec_db.skip_unstarted_executions_for_automations(
                        paused_ids,
                        reason="watchlist_policy",
                        error_message="Skipped unstarted execution because tier policy paused task.",
                        conn=conn,
                    )

        if price_touched:
            await _nudge_price_monitor()

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
        _preclaimed_job: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute Stage 2 (per-company transactions) + Stage 3 (Home YAML sync) for one outbox job."""
        uid = str(user_id or "").strip()
        jid = str(job_id or "").strip()

        if _preclaimed_job is not None:
            job: dict[str, Any] | None = dict(_preclaimed_job)
        else:
            # Item 7: atomic exclusive claim so concurrent workers never double-execute the same job
            job = await claim_reconcile_job(uid, jid)
            if job is None:
                existing = await get_reconcile_job(uid, jid)
                if existing is None:
                    return {"ok": False, "error": "job_not_found", "job_id": jid}
                if existing.get("status") == "completed" and existing.get("yaml_synced"):
                    return {
                        "ok": True,
                        "job_id": jid,
                        "status": "completed",
                        "already_completed": True,
                        "yaml_synced": True,
                        "completed_tickers": list(existing.get("completed_tickers") or []),
                        "pending_tickers": [],
                        "failed_items": [],
                        "company_results": [],
                        "job": existing,
                    }
                return {
                    "ok": True,
                    "job_id": jid,
                    "status": str(existing.get("status") or "running"),
                    "claimed": False,
                    "skipped": True,
                    "reason": "already_claimed",
                    "yaml_synced": bool(existing.get("yaml_synced", False)),
                    "completed_tickers": list(existing.get("completed_tickers") or []),
                    "pending_tickers": list(existing.get("pending_tickers") or []),
                    "failed_items": list(existing.get("failed_items") or []),
                    "company_results": [],
                    "job": existing,
                }

        claim_token = (
            str(job.get("claim_token")).strip()
            if job.get("claim_token") is not None and str(job.get("claim_token")).strip()
            else None
        )
        job_roster_revision = int(job.get("roster_revision") or 0)
        target_versions = dict(job.get("target_policy_versions") or {})
        override_user_pause = bool(job.get("override_user_pause", False))
        job_ctx = dict(job.get("job_context") or {}) if isinstance(job.get("job_context"), Mapping) else {}
        removed_workspaces = (
            job_ctx.get("removed_workspaces")
            if isinstance(job_ctx.get("removed_workspaces"), Mapping)
            else {}
        )
        raw_override_pv = job_ctx.get("override_pause_versions")
        override_pause_versions: dict[str, int] | None = (
            {str(k): int(v) for k, v in raw_override_pv.items()}
            if isinstance(raw_override_pv, Mapping)
            else None
        )

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
            # r5: verify & extend active lease BEFORE executing per-company side effects
            if claim_token is not None:
                still_held = await renew_reconcile_job_lease(
                    uid, jid, claim_token=claim_token
                )
                if not still_held:
                    return {
                        "ok": False,
                        "claimed": False,
                        "job_id": jid,
                        "status": "running",
                        "reason": "lease_lost",
                        "yaml_synced": bool(job.get("yaml_synced", False)),
                        "completed_tickers": completed,
                        "pending_tickers": remaining_pending,
                        "failed_items": new_failed,
                        "company_results": company_results,
                        "job": None,
                    }
            try:
                res = await self.reconcile_company(
                    uid,
                    sym,
                    removed_workspace_id=(
                        str(removed_workspaces.get(sym)).strip()
                        if removed_workspaces.get(sym)
                        else None
                    ),
                    override_user_pause=override_user_pause,
                    override_pause_versions=override_pause_versions,
                    target_policy_versions=target_versions,
                    job_id=jid if claim_token is not None else None,
                    claim_token=claim_token,
                )
                if isinstance(res, Mapping) and (
                    res.get("lease_lost") or res.get("reason") == "lease_lost"
                ):
                    return {
                        "ok": False,
                        "claimed": False,
                        "job_id": jid,
                        "status": "running",
                        "reason": "lease_lost",
                        "yaml_synced": bool(job.get("yaml_synced", False)),
                        "completed_tickers": completed,
                        "pending_tickers": remaining_pending,
                        "failed_items": new_failed,
                        "company_results": company_results,
                        "job": None,
                    }
                company_results.append(res)
                completed.append(sym)
                completed_set.add(sym)
                remaining_pending = [x for x in remaining_pending if x != sym]
                prog = await update_reconcile_job_progress(
                    uid,
                    jid,
                    pending_tickers=remaining_pending,
                    completed_tickers=completed,
                    failed_items=new_failed,
                    claim_token=claim_token,
                    release_claim=False,
                )
                if claim_token is not None and prog is None:
                    return {
                        "ok": False,
                        "claimed": False,
                        "job_id": jid,
                        "status": "running",
                        "reason": "lease_lost",
                        "yaml_synced": bool(job.get("yaml_synced", False)),
                        "completed_tickers": completed,
                        "pending_tickers": remaining_pending,
                        "failed_items": new_failed,
                        "company_results": company_results,
                        "job": None,
                    }
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
                # Decision 2: record `partial` WITHOUT releasing claim_token so lease stays exclusive
                prog = await update_reconcile_job_progress(
                    uid,
                    jid,
                    pending_tickers=remaining_pending,
                    completed_tickers=completed,
                    failed_items=new_failed,
                    status="partial",
                    claim_token=claim_token,
                    release_claim=False,
                )
                if claim_token is not None and prog is None:
                    return {
                        "ok": False,
                        "claimed": False,
                        "job_id": jid,
                        "status": "partial",
                        "reason": "lease_lost",
                        "yaml_synced": bool(job.get("yaml_synced", False)),
                        "completed_tickers": completed,
                        "pending_tickers": remaining_pending,
                        "failed_items": new_failed,
                        "company_results": company_results,
                        "job": None,
                    }

        # Stage 3: Home YAML sync (verify active lease BEFORE, inside YAML lock, and right before actual write)
        yaml_synced = bool(job.get("yaml_synced", False))
        if sync_home and not yaml_synced:
            if claim_token is not None:
                still_held_yaml = await renew_reconcile_job_lease(
                    uid, jid, claim_token=claim_token
                )
                if not still_held_yaml:
                    return {
                        "ok": False,
                        "claimed": False,
                        "job_id": jid,
                        "status": "running",
                        "reason": "lease_lost",
                        "yaml_synced": False,
                        "completed_tickers": completed,
                        "pending_tickers": remaining_pending,
                        "failed_items": new_failed,
                        "company_results": company_results,
                        "job": None,
                    }
            import inspect as _inspect

            yaml_lease_lost = False

            async def _verify_yaml_lease(lock_conn: Any = None) -> bool:
                nonlocal yaml_lease_lost
                if claim_token is None:
                    return True
                sql_c = (
                    lock_conn
                    if (lock_conn is not None and callable(getattr(lock_conn, "cursor", None)))
                    else None
                )
                held = await renew_reconcile_job_lease(
                    uid, jid, claim_token=claim_token, conn=sql_c
                )
                if not held:
                    yaml_lease_lost = True
                return held

            sync_kwargs: dict[str, Any] = {}
            backend_resolve_failed = False
            try:
                _sync_params = _inspect.signature(sync_user_coverage_to_home).parameters
                _has_var_kw = any(
                    p.kind == _inspect.Parameter.VAR_KEYWORD for p in _sync_params.values()
                )
                if "roster_revision" in _sync_params or _has_var_kw:
                    sync_kwargs["roster_revision"] = job_roster_revision
                if claim_token is not None and ("lease_check" in _sync_params or _has_var_kw):
                    sync_kwargs["lease_check"] = _verify_yaml_lease
                if "backend" in _sync_params or _has_var_kw:
                    from src.dalton.coverage import service as coverage_svc

                    # Pre-resolve Home sandbox backend BEFORE entering sync_user_coverage_to_home's
                    # YAML advisory lock so pool max_size=1 never deadlocks on a second checkout.
                    if sync_user_coverage_to_home is coverage_svc.sync_user_coverage_to_home:
                        try:
                            sync_kwargs["backend"] = await coverage_svc._resolve_home_sandbox_backend(
                                uid
                            )
                        except Exception as exc:
                            logger.info(
                                "[WATCHLIST_RECONCILE] skipped or failed resolving Home backend for user=%s: %s",
                                uid,
                                exc,
                            )
                            backend_resolve_failed = True
            except (TypeError, ValueError):
                sync_kwargs["roster_revision"] = job_roster_revision
            synced = (
                False
                if backend_resolve_failed
                else bool(await sync_user_coverage_to_home(uid, **sync_kwargs))
            )
            if yaml_lease_lost:
                return {
                    "ok": False,
                    "claimed": False,
                    "job_id": jid,
                    "status": "running",
                    "reason": "lease_lost",
                    "yaml_synced": False,
                    "completed_tickers": completed,
                    "pending_tickers": remaining_pending,
                    "failed_items": new_failed,
                    "company_results": company_results,
                    "job": None,
                }
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
            claim_token=claim_token,
            release_claim=True,
        )
        if claim_token is not None and updated_job is None:
            return {
                "ok": False,
                "claimed": False,
                "job_id": jid,
                "status": final_status,
                "reason": "lease_lost",
                "yaml_synced": yaml_synced,
                "completed_tickers": completed,
                "pending_tickers": remaining_pending,
                "failed_items": new_failed,
                "company_results": company_results,
                "job": None,
            }
        return {
            "ok": final_status == "completed",
            "claimed": True,
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
        all_done = all(
            r.get("status") == "completed" or r.get("reason") == "already_claimed"
            for r in results
        )
        return {
            "ok": all_done,
            "processed_jobs": len(results),
            "results": results,
        }

    async def mark_yaml_synced_for_user(
        self,
        user_id: str,
        *,
        covered_job_ids: Sequence[str] | None = None,
        max_revision: int | None = None,
    ) -> int:
        """Mark covered completed unsynced outbox jobs as ``yaml_synced=TRUE`` after Home sync."""
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        allowed_ids = (
            {str(jid).strip() for jid in covered_job_ids if str(jid).strip()}
            if covered_job_ids is not None
            else None
        )
        if allowed_ids is not None and not allowed_ids:
            return 0
        jobs = await list_unfinished_reconcile_jobs(uid)
        updated = 0
        for job in jobs:
            jid = str(job["job_id"])
            if allowed_ids is not None and jid not in allowed_ids:
                continue
            if job.get("yaml_synced"):
                continue
            if max_revision is not None and int(job.get("roster_revision") or 0) > int(max_revision):
                continue
            pending = list(job.get("pending_tickers") or [])
            failed = list(job.get("failed_items") or [])
            # Decision 6 / r5: NEVER mark unfinished, failed, running, or currently-leased jobs as yaml_synced=TRUE
            if pending or failed:
                continue
            if job.get("claim_token") or str(job.get("status") or "").strip().lower() == "running":
                continue
            res_upd = await update_reconcile_job_progress(
                uid,
                jid,
                yaml_synced=True,
                status="completed",
                only_unclaimed_completed=True,
            )
            if res_upd is not None:
                updated += 1
        return updated


async def run_startup_reconcile_scan(
    *,
    limit: int = 100,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Drain all claimable unfinished watchlist outbox jobs just-in-time without pre-claiming stale batches."""
    reconciler = TierPolicyReconciler()
    # r5: default to claiming 1 job just-in-time so queued jobs never sit idle in a pre-claimed batch past lease TTL
    eff_batch = max(1, int(batch_size)) if batch_size is not None else 1
    _ = limit
    seen_job_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    while True:
        try:
            claimed_jobs = await claim_unfinished_reconcile_jobs_for_startup(
                limit=eff_batch,
                exclude_job_ids=sorted(seen_job_ids) if seen_job_ids else None,
            )
        except Exception as exc:
            logger.debug("[WATCHLIST_RECONCILE] startup scan stopped: %s", exc)
            if not results:
                return {"ok": False, "processed_jobs": 0, "error": type(exc).__name__}
            break

        if not claimed_jobs:
            break

        progress_this_batch = False
        for job in claimed_jobs:
            uid = str(job.get("user_id") or "").strip()
            jid = str(job.get("job_id") or "").strip()
            if not uid or not jid or jid in seen_job_ids:
                continue
            seen_job_ids.add(jid)
            progress_this_batch = True
            try:
                res = await reconciler.reconcile_job(
                    uid, jid, sync_home=True, _preclaimed_job=job
                )
                results.append(res)
            except Exception as exc:
                logger.warning(
                    "[WATCHLIST_RECONCILE] startup reconcile job failed | user=%s job=%s exc=%s",
                    uid,
                    jid,
                    type(exc).__name__,
                )
                results.append({"ok": False, "job_id": jid, "error": type(exc).__name__})

        if not progress_this_batch:
            break

    return {
        "ok": all(r.get("ok") for r in results),
        "processed_jobs": len(results),
        "results": results,
    }


__all__ = [
    "AUTOMATIONS_RELOAD_SIGNAL",
    "STANDARD_TASK_SUFFIXES",
    "TierPolicyReconciler",
    "resolve_company_workspace_id",
    "run_startup_reconcile_scan",
]
