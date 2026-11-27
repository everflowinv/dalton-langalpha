"""Shared WatchlistMutationService: unified preview, snapshot drift check, and Stage-1 outbox writes (§5.3, §5.4)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from src.dalton.autonomy.schedule import build_company_watch_specs, list_all_user_automations
from src.dalton.autonomy.tracking import resolve_workspace_tracking_owner
from src.dalton.coverage.models import (
    VALID_TIERS,
    extract_row_group_ids,
    extract_row_stale_group_ids,
    normalize_source,
    normalize_tags,
    normalize_tier,
    row_to_company,
)
from src.dalton.coverage.service import (
    _build_upsert_rows_from_existing,
    _user_workspace_tickers,
    ensure_user_earnings_calendar_automation,
    invalidate_user_aliases_cache,
    preview_coverage_group,
    validate_and_enrich_tickers,
)
from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist.pause_guards import is_earnings_task_allowed_for_stages
from src.dalton.watchlist.policies import (
    TIER_POLICY_MUTABLE_FIELDS,
    TierPolicy,
    bump_user_roster_revision,
    consume_confirm_snapshot,
    diff_tier_policies,
    extract_tier_policy_patch,
    get_confirm_snapshot,
    get_default_tier_policy,
    get_user_roster_state,
    get_user_tier_policies,
    insert_confirm_snapshot,
    insert_reconcile_job,
    resolve_effective_company_policy,
    upsert_tier_policy,
    validate_tier_policy_update,
)
from src.dalton.watchlist.reconciler import (
    STANDARD_TASK_SUFFIXES,
    TierPolicyReconciler,
    _company_rows_for_workspace,
    _earn_stage_from_name,
    _find_standard_task_row,
    _has_handoff_pause,
    _has_policy_pause,
    _has_user_pause,
    _is_future_earn_once,
    _is_replaced_by_tracker,
    resolve_company_workspace_id,
)

logger = logging.getLogger(__name__)

# Maximum number of net-new tickers allowed for confirmation-free `add` (§5.4)
EXEMPT_ADD_MAX_TICKERS = 5

WATCHLIST_CONFIRM_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "watchlist_batch_add",
        "watchlist_set_tier",
        "watchlist_remove",
        "watchlist_tier_policy",
    }
)

TIER_DISPLAY_ZH: dict[str, str] = {
    "core": "核心 (Core)",
    "watch": "跟踪 (Watch)",
    "radar": "雷达 (Radar)",
}


class WatchlistScopeChangedError(RuntimeError):
    """Raised when DB state drifted between preview snapshot and Stage-1 write transaction."""

    code = "scope_changed"

    def __init__(self, message: str = "情况已变化，请重新发起") -> None:
        super().__init__(message)
        self.message = message


def normalize_tickers_input(tickers: Sequence[str] | str | None) -> list[str]:
    """Normalize comma/semicolon-separated string or sequence of tickers into deduplicated uppercase list."""
    if tickers is None:
        return []
    raw_items: list[str] = []
    if isinstance(tickers, str):
        raw_items.extend(tickers.replace(";", ",").split(","))
    elif isinstance(tickers, (list, tuple, set)):
        for item in tickers:
            if isinstance(item, str):
                raw_items.extend(item.replace(";", ",").split(","))
    seen: set[str] = set()
    out: list[str] = []
    for part in raw_items:
        sym = part.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def compute_roster_hash(
    tickers: Sequence[str],
    existing_by_ticker: Mapping[str, Mapping[str, Any]],
    ws_by_ticker: Mapping[str, str | None],
    *,
    ws_owners: Mapping[str, str | None] | None = None,
) -> str:
    """Deterministic SHA-256 fingerprint over the target tickers' current roster & workspace ownership state."""
    records: list[dict[str, Any]] = []
    for sym in sorted({str(t).strip().upper() for t in tickers if str(t).strip()}):
        row = existing_by_ticker.get(sym)
        rec: dict[str, Any] = {
            "ticker": sym,
            "exists": row is not None,
            "tier": str(row.get("tier") or "").strip().lower() if row is not None else None,
            "is_private": bool(row.get("is_private", False)) if row is not None else False,
            "workspace_id": ws_by_ticker.get(sym),
        }
        if ws_owners is not None:
            rec["workspace_owner_id"] = ws_owners.get(sym)
        records.append(rec)
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _collect_user_pause_versions(
    company_rows: Sequence[Mapping[str, Any]],
    *,
    ticker: str,
) -> dict[str, int]:
    """Collect ``{automation_id: config_version}`` for user-paused standard tasks in a company workspace."""
    out: dict[str, int] = {}
    for suffix in STANDARD_TASK_SUFFIXES:
        existing = _find_standard_task_row(company_rows, ticker, suffix)
        if existing is None:
            continue
        if _has_user_pause(existing):
            auto_id = str(existing.get("automation_id") or "").strip()
            if auto_id:
                out[auto_id] = int(existing.get("config_version") or 1)
    return out


def _estimate_workspace_task_changes(
    *,
    ticker: str,
    workspace_id: str,
    old_tier: str | None,
    new_tier: str | None,
    new_policy: TierPolicy | None,
    company_rows: Sequence[Mapping[str, Any]],
    override_user_pause: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Compute read-only preview of automation changes for one company workspace."""
    counts = {
        "create_count": 0,
        "update_count": 0,
        "resume_count": 0,
        "pause_count": 0,
        "preserved_user_pause_count": 0,
        "preserved_tracker_count": 0,
        "preserved_handoff_count": 0,
    }
    changes: list[dict[str, Any]] = []
    t_lower = ticker.lower()
    std_names = {s: f"dalton-{t_lower}-{s}" for s in STANDARD_TASK_SUFFIXES}

    if new_tier is None or new_policy is None:
        # Remove action: pause all existing managed standard tasks + future earn tasks
        std_row_ids: set[str] = set()
        for suffix in STANDARD_TASK_SUFFIXES:
            existing = _find_standard_task_row(company_rows, ticker, suffix)
            if existing is not None:
                r_name = str(existing.get("name") or std_names[suffix]).strip().lower()
                if str(existing.get("status") or "active").strip().lower() != "disabled":
                    counts["pause_count"] += 1
                    changes.append({"name": r_name, "change": "pause"})
                if existing.get("automation_id"):
                    std_row_ids.add(str(existing["automation_id"]))
        for r in company_rows:
            if r.get("automation_id") and str(r["automation_id"]) in std_row_ids:
                continue
            r_name = str(r.get("name") or "").strip().lower()
            if _is_future_earn_once(r):
                counts["pause_count"] += 1
                changes.append({"name": r_name, "change": "pause"})
        return changes, counts

    desired = build_company_watch_specs(
        ticker=ticker,
        workspace_id=workspace_id,
        tier=new_tier,
        policy=new_policy,
    )
    spec_by_name = {str(s["name"]).lower(): s for s in desired}
    company_paused_by_user = any(
        _has_user_pause(row)
        for suffix in STANDARD_TASK_SUFFIXES
        if (row := _find_standard_task_row(company_rows, ticker, suffix)) is not None
    )

    for suffix, s_name in std_names.items():
        existing = _find_standard_task_row(company_rows, ticker, suffix)
        spec = spec_by_name.get(s_name)
        if spec is None:
            if existing is not None and str(existing.get("status") or "active").strip().lower() != "disabled":
                counts["pause_count"] += 1
                changes.append({"name": str(existing.get("name") or s_name).lower(), "change": "pause"})
            continue
        if existing is None:
            if company_paused_by_user and not override_user_pause:
                counts["preserved_user_pause_count"] += 1
                changes.append({"name": s_name, "change": "preserve_user_pause"})
            else:
                counts["create_count"] += 1
                changes.append(
                    {
                        "name": s_name,
                        "change": "create",
                        "cron": spec.get("cron_expression"),
                    }
                )
            continue
        status_now = str(existing.get("status") or "active").strip().lower()
        if status_now == "disabled":
            continue
        if _is_replaced_by_tracker(existing):
            counts["preserved_tracker_count"] += 1
            changes.append({"name": s_name, "change": "preserve_replaced_by_tracker"})
            continue
        if _has_handoff_pause(existing):
            counts["preserved_handoff_count"] += 1
            changes.append({"name": s_name, "change": "preserve_handoff_pause"})
            continue
        if _has_user_pause(existing) and not override_user_pause:
            counts["preserved_user_pause_count"] += 1
            changes.append({"name": s_name, "change": "preserve_user_pause"})
            continue
        if status_now == "paused" and (_has_policy_pause(existing) or override_user_pause):
            counts["resume_count"] += 1
            changes.append(
                {
                    "name": s_name,
                    "change": "resume",
                    "cron": spec.get("cron_expression"),
                }
            )
        elif status_now == "active":
            is_price = str(spec.get("trigger_type") or "").strip().lower() == "price"
            if is_price:
                old_cfg = existing.get("trigger_config") if isinstance(existing.get("trigger_config"), Mapping) else {}
                old_conds = old_cfg.get("conditions") if isinstance(old_cfg.get("conditions"), list) else []
                old_val = old_conds[0].get("value") if old_conds and isinstance(old_conds[0], Mapping) else None
                new_cfg = spec.get("trigger_config") if isinstance(spec.get("trigger_config"), Mapping) else {}
                new_conds = new_cfg.get("conditions") if isinstance(new_cfg.get("conditions"), list) else []
                new_val = new_conds[0].get("value") if new_conds and isinstance(new_conds[0], Mapping) else None
                old_num = round(float(old_val), 2) if isinstance(old_val, (int, float)) else None
                new_num = round(float(new_val), 2) if isinstance(new_val, (int, float)) else None
                if old_num != new_num or old_tier != new_tier:
                    counts["update_count"] += 1
                    changes.append(
                        {
                            "name": s_name,
                            "change": "update",
                            "old_threshold_pct": abs(old_num) if old_num is not None else None,
                            "new_threshold_pct": abs(new_num) if new_num is not None else None,
                        }
                    )
            else:
                old_cron = existing.get("cron_expression")
                new_cron = spec.get("cron_expression")
                old_tz = existing.get("timezone")
                new_tz = spec.get("timezone")
                if old_cron != new_cron or (new_tz and old_tz != new_tz) or old_tier != new_tier:
                    counts["update_count"] += 1
                    changes.append(
                        {
                            "name": s_name,
                            "change": "update",
                            "old_cron": old_cron,
                            "new_cron": new_cron,
                            "old_timezone": old_tz,
                            "new_timezone": new_tz,
                        }
                    )

    # Preview future earnings stage pauses using the same shared rules as reconciler
    allowed_stages = set(new_policy.earnings_stages)
    for r in company_rows:
        if not _is_future_earn_once(r):
            continue
        r_name = str(r.get("name") or "").strip().lower()
        earn_kind = _earn_stage_from_name(r_name, ticker)
        if not is_earnings_task_allowed_for_stages(earn_kind, allowed_stages):
            counts["pause_count"] += 1
            changes.append({"name": r_name, "change": "pause"})

    return changes, counts


async def _resolve_ws_map_and_owners(
    user_id: str,
    tickers: Sequence[str],
    existing_by_ticker: Mapping[str, Mapping[str, Any]],
    *,
    owned_ws: Mapping[str, str] | None = None,
    conn: Any = None,
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    ws_fallback = owned_ws if owned_ws is not None else await _user_workspace_tickers(user_id)
    ws_map: dict[str, str | None] = {}
    ws_owners: dict[str, str | None] = {}
    for sym in tickers:
        row = existing_by_ticker.get(sym)
        ws_id = await resolve_company_workspace_id(
            user_id, sym, cov_row=row, conn=conn
        )
        if not ws_id:
            ws_id = ws_fallback.get(sym)
        ws_map[sym] = ws_id
        if ws_id:
            try:
                ws_owners[sym] = await resolve_workspace_tracking_owner(ws_id, conn=conn)
            except RuntimeError as exc:
                if "database pool is not open" not in str(exc):
                    raise
                ws_owners[sym] = user_id
        else:
            ws_owners[sym] = None
    return ws_map, ws_owners


def _compute_resolve_group_membership_delta(
    group_id: str,
    matched_set: set[str],
    existing_rows: Sequence[Mapping[str, Any]],
    existing_by_ticker: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Compute the full deterministic membership delta for ``resolve_group`` (preview & locked execute)."""
    gid = str(group_id or "").strip()
    new_tickers = sorted(t for t in matched_set if t not in existing_by_ticker)
    membership_tickers = sorted(
        t
        for t in matched_set
        if t not in existing_by_ticker
        or gid not in extract_row_group_ids(existing_by_ticker[t])
    )
    stale_tickers = sorted(
        str(r["ticker"]).upper()
        for r in existing_rows
        if r.get("ticker")
        and gid in extract_row_group_ids(r)
        and str(r["ticker"]).upper() not in matched_set
    )
    unstale_tickers = sorted(
        str(r["ticker"]).upper()
        for r in existing_rows
        if r.get("ticker")
        and gid in extract_row_group_ids(r)
        and str(r["ticker"]).upper() in matched_set
        and gid in extract_row_stale_group_ids(r)
    )
    return {
        "new_tickers": new_tickers,
        "membership_tickers": membership_tickers,
        "stale_tickers": stale_tickers,
        "unstale_tickers": unstale_tickers,
    }


def _normalize_resolve_membership_delta(raw: Mapping[str, Any]) -> dict[str, list[str]]:
    """Normalize a snapshot's ``resolve_membership_delta`` mapping for exact comparison."""
    out: dict[str, list[str]] = {}
    for key in ("new_tickers", "membership_tickers", "stale_tickers", "unstale_tickers"):
        vals = raw.get(key)
        if not isinstance(vals, Sequence) or isinstance(vals, (str, bytes)):
            raise WatchlistScopeChangedError()
        out[key] = sorted(
            str(t).strip().upper() for t in vals if str(t).strip()
        )
    return out


class WatchlistMutationService:
    """Unified preview & transactional mutation service shared by Web REST, legacy `/coverage`, and CoS."""

    def __init__(self, reconciler: TierPolicyReconciler | None = None) -> None:
        self._reconciler = reconciler or TierPolicyReconciler()

    async def preview_mutation(
        self,
        user_id: str,
        *,
        action: str,
        tickers: Sequence[str] | str | None = None,
        tier: str | None = None,
        policy: Mapping[str, Any] | None = None,
        label: str | None = None,
        criteria: Mapping[str, Any] | None = None,
        default_tier: str | None = None,
        auto_refresh: bool = True,
        group_id: str | None = None,
        tags: Sequence[str] | None = None,
        override_user_pause: bool = False,
        tool_call_id: str = "",
        skip_ticker_validation: bool = False,
    ) -> dict[str, Any]:
        """Pure read-only preview of a personal watchlist mutation.

        Returns a structured snapshot suitable for both web confirmation modals
        and ``StandingApprovalMiddleware.approval_snapshots[tool_call_id]``.
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        act = str(action or "").strip().lower()

        if act in ("list", "preview_group", "get_tier_policy"):
            return {
                "ok": True,
                "raw_action": act,
                "action": None,
                "requires_confirmation": False,
                "owner_user_id": uid,
            }

        if act == "update_tags":
            norm_tickers = normalize_tickers_input(tickers)
            return {
                "ok": True,
                "raw_action": act,
                "action": None,
                "requires_confirmation": False,
                "owner_user_id": uid,
                "target_tickers": norm_tickers,
                "tags": normalize_tags(tags),
            }

        existing_rows = await coverage_db.list_coverage_rows(uid)
        existing_by_ticker = {
            str(r["ticker"]).strip().upper(): r for r in existing_rows if r.get("ticker")
        }
        policies = await get_user_tier_policies(uid)
        user_roster_state = await get_user_roster_state(uid)
        roster_revision = int(user_roster_state.get("roster_revision") or 0)

        # Resolve target tickers & tier according to action
        raw_tickers: list[str] = []
        norm_tickers: list[str] = []
        not_found: list[str] = []
        prevalidated: dict[str, dict[str, Any]] = {}
        target_tier: str | None = None
        resolved_group_id: str | None = None
        resolved_group_label: str | None = None
        resolved_criteria: dict[str, Any] = dict(criteria or {})
        before_tier_policy: TierPolicy | None = None
        proposed_tier_policy: TierPolicy | None = None
        policy_diff: dict[str, Any] | None = None
        policies_for_preview: dict[str, TierPolicy] = dict(policies)

        resolve_membership_delta: dict[str, list[str]] | None = None
        if act == "add":
            raw_tickers = normalize_tickers_input(tickers)
            if not raw_tickers:
                raise ValueError("执行 add 时必须提供 tickers。")
            target_tier = normalize_tier(tier, default="watch")
            if skip_ticker_validation:
                norm_tickers = list(raw_tickers)
                prevalidated = {
                    t: {"ticker": t, "name": t} for t in raw_tickers
                }
            else:
                valid_items, not_found = await validate_and_enrich_tickers(raw_tickers)
                norm_tickers = [str(item["ticker"]).upper() for item in valid_items]
                prevalidated = {
                    str(item["ticker"]).upper(): dict(item) for item in valid_items
                }

        elif act == "set_tier":
            raw_tickers = normalize_tickers_input(tickers)
            if not raw_tickers:
                raise ValueError("执行 set_tier 时必须提供 tickers。")
            target_tier = normalize_tier(tier, default="watch")
            for sym in raw_tickers:
                if sym in existing_by_ticker:
                    norm_tickers.append(sym)
                else:
                    not_found.append(sym)

        elif act == "remove":
            raw_tickers = normalize_tickers_input(tickers)
            if not raw_tickers:
                raise ValueError("执行 remove 时必须提供 tickers。")
            for sym in raw_tickers:
                if sym in existing_by_ticker:
                    norm_tickers.append(sym)
                else:
                    not_found.append(sym)

        elif act == "add_group":
            clean_label = str(label or "").strip()
            if not clean_label:
                raise ValueError("执行 add_group 时必须提供 label。")
            resolved_group_label = clean_label
            target_tier = normalize_tier(default_tier or tier, default="watch")
            grp_preview = await preview_coverage_group(clean_label, resolved_criteria)
            resolved_companies = grp_preview.get("companies") or []
            not_found = list(grp_preview.get("not_found") or [])
            norm_tickers = [
                str(c["ticker"]).upper() for c in resolved_companies if c.get("ticker")
            ]
            raw_tickers = list(norm_tickers)
            prevalidated = {
                str(c["ticker"]).upper(): dict(c)
                for c in resolved_companies
                if c.get("ticker")
            }

        elif act == "remove_group":
            target_gid = str(group_id or label or "").strip()
            if not target_gid:
                raise ValueError("执行 remove_group 时必须提供 group_id（或 label）。")
            groups = await coverage_db.list_coverage_groups(uid)
            matched_group = next(
                (g for g in groups if str(g.get("group_id") or "") == target_gid),
                None,
            )
            if matched_group is None:
                matched_group = next(
                    (
                        g
                        for g in groups
                        if str(g.get("label") or "").strip().lower() == target_gid.lower()
                    ),
                    None,
                )
            if matched_group is None:
                return {
                    "ok": False,
                    "raw_action": act,
                    "action": None,
                    "requires_confirmation": False,
                    "error": "group_not_found",
                    "precheck_error": f"未找到规则组 `{target_gid}`。",
                    "owner_user_id": uid,
                }
            resolved_group_id = str(matched_group["group_id"])
            resolved_group_label = str(matched_group.get("label") or resolved_group_id)
            norm_tickers = coverage_db.compute_exclusive_group_deletion_tickers(
                resolved_group_id, groups, existing_rows
            )
            raw_tickers = list(norm_tickers)

        elif act == "resolve_group":
            target_gid = str(group_id or label or "").strip()
            if not target_gid:
                raise ValueError("执行 resolve_group 时必须提供 group_id。")
            matched_group = await coverage_db.get_coverage_group(uid, target_gid)
            if matched_group is None:
                return {
                    "ok": False,
                    "raw_action": act,
                    "action": None,
                    "requires_confirmation": False,
                    "error": "group_not_found",
                    "precheck_error": f"未找到规则组 `{target_gid}`。",
                    "owner_user_id": uid,
                }
            resolved_group_id = str(matched_group["group_id"])
            resolved_group_label = str(matched_group.get("label") or resolved_group_id)
            resolved_criteria = dict(matched_group.get("criteria") or {})
            target_tier = normalize_tier(matched_group.get("default_tier"), default="watch")
            grp_preview = await preview_coverage_group(resolved_group_label, resolved_criteria)
            resolved_companies = grp_preview.get("companies") or []
            not_found = list(grp_preview.get("not_found") or [])
            matched_all = [
                str(c["ticker"]).upper() for c in resolved_companies if c.get("ticker")
            ]
            resolve_membership_delta = _compute_resolve_group_membership_delta(
                resolved_group_id, set(matched_all), existing_rows, existing_by_ticker
            )
            norm_tickers = list(resolve_membership_delta["new_tickers"])
            raw_tickers = list(norm_tickers)
            prevalidated = {
                str(c["ticker"]).upper(): dict(c)
                for c in resolved_companies
                if c.get("ticker")
            }

        elif act in ("set_tier_policy", "reset_tier_policy"):
            if not str(tier or "").strip():
                raise ValueError(f"执行 {act} 时必须指定 tier（core / watch / radar）。")
            target_tier = normalize_tier(tier)
            before_tier_policy = policies.get(target_tier, get_default_tier_policy(target_tier))
            if act == "set_tier_policy":
                proposed_tier_policy = validate_tier_policy_update(before_tier_policy, policy)
            else:
                proposed_tier_policy = get_default_tier_policy(target_tier)
            policy_diff = diff_tier_policies(before_tier_policy, proposed_tier_policy)
            policies_for_preview[target_tier] = proposed_tier_policy
            norm_tickers = sorted(
                sym
                for sym, row in existing_by_ticker.items()
                if str(row.get("tier") or "").strip().lower() == target_tier
            )
            raw_tickers = list(norm_tickers)

        else:
            raise ValueError(f"Unsupported watchlist mutation action: {action!r}")

        requested_count = len(raw_tickers)
        ws_by_ticker, ws_owners = await _resolve_ws_map_and_owners(
            uid, norm_tickers, existing_by_ticker
        )

        # Load user's automations once for read-only task-change preview
        try:
            all_autos = await list_all_user_automations(uid)
        except Exception:
            all_autos = []

        involved_tiers: set[str] = set()
        if target_tier in VALID_TIERS:
            involved_tiers.add(target_tier)
        for sym in norm_tickers:
            row = existing_by_ticker.get(sym)
            if row is not None:
                rt = str(row.get("tier") or "").strip().lower()
                if rt in VALID_TIERS:
                    involved_tiers.add(rt)
        expected_policy_versions = {
            t: policies.get(t, get_default_tier_policy(t)).policy_version
            for t in sorted(involved_tiers)
        }
        expected_roster_hash = compute_roster_hash(
            norm_tickers, existing_by_ticker, ws_by_ticker, ws_owners=ws_owners
        )

        affected_tickers: list[dict[str, Any]] = []
        affected_workspaces: list[dict[str, Any]] = []
        expected_pause_versions: dict[str, int] = {}
        task_summary = {
            "create_count": 0,
            "update_count": 0,
            "resume_count": 0,
            "pause_count": 0,
            "preserved_user_pause_count": 0,
            "preserved_tracker_count": 0,
            "preserved_handoff_count": 0,
        }

        for sym in norm_tickers:
            row = existing_by_ticker.get(sym)
            old_t = str(row.get("tier") or "").strip().lower() if row is not None else None
            # For add_group, existing rows preserve their manual tier
            if act == "add_group" and row is not None:
                eff_new_tier = old_t
            else:
                eff_new_tier = target_tier
            ws_id = ws_by_ticker.get(sym)
            is_priv = bool(row.get("is_private", False)) if row is not None else False
            name = (
                str(row.get("name") or sym)
                if row is not None
                else str((prevalidated.get(sym) or {}).get("name") or sym)
            )
            affected_tickers.append(
                {
                    "ticker": sym,
                    "name": name,
                    "old_tier": old_t,
                    "new_tier": eff_new_tier,
                    "has_workspace": bool(ws_id),
                    "workspace_id": ws_id,
                    "is_private": is_priv,
                }
            )
            if ws_id and not is_priv:
                c_rows = _company_rows_for_workspace(all_autos, workspace_id=ws_id, ticker=sym)
                if override_user_pause:
                    expected_pause_versions.update(
                        _collect_user_pause_versions(c_rows, ticker=sym)
                    )
                eff_pol = (
                    resolve_effective_company_policy(
                        policies_for_preview.get(
                            eff_new_tier, get_default_tier_policy(eff_new_tier)
                        ),
                        row.get("task_overrides") if row is not None else None,
                    )
                    if eff_new_tier is not None
                    else None
                )
                changes, counts = _estimate_workspace_task_changes(
                    ticker=sym,
                    workspace_id=ws_id,
                    old_tier=old_t,
                    new_tier=eff_new_tier,
                    new_policy=eff_pol,
                    company_rows=c_rows,
                    override_user_pause=override_user_pause,
                )
                for k, v in counts.items():
                    task_summary[k] += v
                affected_workspaces.append(
                    {
                        "ticker": sym,
                        "workspace_id": ws_id,
                        "old_tier": old_t,
                        "new_tier": eff_new_tier,
                        "task_changes": changes,
                    }
                )

        affected_workspaces.sort(key=lambda w: w["ticker"])

        # Determine confirmation requirement & action_type (§5.4, Items 2 & 4)
        # Exempt ONLY when:
        #   action == "add"
        #   AND requested ticker count <= 5 (NOT validated count)
        #   AND all requested tickers are net-new (none already in roster)
        #   AND override_user_pause is False
        #   AND zero paused tasks would be resumed
        any_existing_in_request = any(sym in existing_by_ticker for sym in raw_tickers)
        changing_existing_tiers = [
            sym
            for sym in norm_tickers
            if sym in existing_by_ticker
            and target_tier is not None
            and str(existing_by_ticker[sym].get("tier") or "").lower() != target_tier
        ]
        resuming_any_paused = task_summary["resume_count"] > 0

        confirm_action_type: str | None = None
        requires_confirmation = False

        if act == "add":
            if (
                not any_existing_in_request
                and requested_count <= EXEMPT_ADD_MAX_TICKERS
                and not override_user_pause
                and not resuming_any_paused
            ):
                confirm_action_type = None
                requires_confirmation = False
            elif changing_existing_tiers or any_existing_in_request or override_user_pause or resuming_any_paused:
                confirm_action_type = "watchlist_set_tier"
                requires_confirmation = True
            else:
                confirm_action_type = "watchlist_batch_add"
                requires_confirmation = True
        elif act in ("add_group", "resolve_group"):
            confirm_action_type = "watchlist_batch_add"
            requires_confirmation = True
        elif act == "set_tier":
            confirm_action_type = "watchlist_set_tier"
            requires_confirmation = True
        elif act in ("remove", "remove_group"):
            confirm_action_type = "watchlist_remove"
            requires_confirmation = True
        elif act in ("set_tier_policy", "reset_tier_policy"):
            confirm_action_type = "watchlist_tier_policy"
            requires_confirmation = True

        # Human-readable card copy
        if confirm_action_type == "watchlist_batch_add":
            tier_lbl = TIER_DISPLAY_ZH.get(target_tier or "watch", target_tier or "watch")
            if act == "add_group":
                card_title = f"确认添加规则组「{resolved_group_label}」"
                card_description = (
                    f"将规则组「{resolved_group_label}」解析出的 {len(norm_tickers)} 只标的加入个人观察名单"
                    f"（默认分档：{tier_lbl}）。"
                )
            else:
                card_title = f"确认批量添加 {len(norm_tickers)} 只标的到观察名单"
                card_description = (
                    f"将 {len(norm_tickers)} 只标的加入个人观察名单（分档：{tier_lbl}）。"
                )
        elif confirm_action_type == "watchlist_set_tier":
            tier_lbl = TIER_DISPLAY_ZH.get(target_tier or "watch", target_tier or "watch")
            card_title = f"确认调整 {len(norm_tickers)} 只标的至 {tier_lbl}"
            card_description = (
                f"调整 {', '.join(norm_tickers[:5])}{' 等' if len(norm_tickers) > 5 else ''} 至 {tier_lbl}，"
                f"影响 {len(affected_workspaces)} 个已有研究工作区的受管任务。"
            )
        elif confirm_action_type == "watchlist_remove":
            if act == "remove_group":
                card_title = f"确认删除规则组「{resolved_group_label}」"
                card_description = (
                    f"删除规则组「{resolved_group_label}」并移除仅属于该组的 {len(norm_tickers)} 只标的；"
                    "研究工作区与历史文档完整保留，关联受管定时任务及待执行财报任务同步暂停。"
                )
            else:
                card_title = f"确认从观察名单移除 {len(norm_tickers)} 只标的"
                card_description = (
                    f"移除 {', '.join(norm_tickers[:5])}{' 等' if len(norm_tickers) > 5 else ''}；"
                    "研究工作区与历史文档完整保留，关联受管定时任务及待执行财报任务同步暂停。"
                )
        elif confirm_action_type == "watchlist_tier_policy":
            tier_lbl = TIER_DISPLAY_ZH.get(target_tier or "watch", target_tier or "watch")
            changed_n = len((policy_diff or {}).get("changed_fields") or [])
            if act == "reset_tier_policy":
                card_title = f"确认恢复 {tier_lbl} 默认监控策略"
                card_description = (
                    f"将 {tier_lbl} 监控策略恢复为默认值，"
                    f"覆盖 {len(norm_tickers)} 家公司、影响 {len(affected_workspaces)} 个已有研究工作区的受管任务。"
                )
            else:
                card_title = f"确认修改 {tier_lbl} 监控策略"
                card_description = (
                    f"更新 {tier_lbl} 监控策略（变更 {changed_n} 项配置），"
                    f"覆盖 {len(norm_tickers)} 家公司、影响 {len(affected_workspaces)} 个已有研究工作区的受管任务。"
                )
        else:
            card_title = ""
            card_description = ""

        snapshot_id: str | None = None
        if requires_confirmation:
            server_snapshot_payload: dict[str, Any] = {
                "owner_user_id": uid,
                "raw_action": act,
                "action": confirm_action_type,
                "requested_count": requested_count,
                "target_tickers": list(norm_tickers),
                "not_found": list(not_found),
                "target_tier": target_tier,
                "group_id": resolved_group_id,
                "group_label": resolved_group_label,
                "criteria": resolved_criteria if act == "add_group" else None,
                "auto_refresh": bool(auto_refresh) if act == "add_group" else None,
                "override_user_pause": bool(override_user_pause),
                "expected_roster_hash": expected_roster_hash,
                "expected_policy_versions": dict(expected_policy_versions),
                "expected_policy_version": (
                    before_tier_policy.policy_version if before_tier_policy is not None else None
                ),
                "expected_pause_versions": dict(expected_pause_versions),
                "roster_revision": roster_revision,
                "prevalidated": prevalidated if act in ("add", "add_group") else None,
                "resolve_membership_delta": resolve_membership_delta if act == "resolve_group" else None,
                "proposed_policy": (
                    proposed_tier_policy.to_patch_dict()
                    if proposed_tier_policy is not None
                    else None
                ),
                "proposed_policy_snapshot": (
                    proposed_tier_policy.to_dict()
                    if proposed_tier_policy is not None
                    else None
                ),
                "policy_diff": policy_diff,
            }
            snapshot_id = await insert_confirm_snapshot(uid, server_snapshot_payload)

        return {
            "ok": True,
            "action": confirm_action_type,
            "raw_action": act,
            "tool_call_id": tool_call_id,
            "snapshot_id": snapshot_id,
            "roster_revision": roster_revision,
            "requires_confirmation": requires_confirmation,
            "owner_user_id": uid,
            "requested_count": requested_count,
            "target_tickers": norm_tickers,
            "target_tier": target_tier,
            "not_found": not_found,
            "expected_roster_hash": expected_roster_hash,
            "expected_policy_version": (
                before_tier_policy.policy_version if before_tier_policy is not None else None
            ),
            "expected_policy_versions": expected_policy_versions,
            "expected_pause_versions": expected_pause_versions,
            "affected_tickers": affected_tickers,
            "affected_workspaces": affected_workspaces,
            "task_summary": task_summary,
            "override_user_pause": bool(override_user_pause),
            "group_id": resolved_group_id,
            "group_label": resolved_group_label,
            "criteria": resolved_criteria if act == "add_group" else None,
            "auto_refresh": bool(auto_refresh) if act == "add_group" else None,
            "prevalidated": prevalidated if act in ("add", "add_group") else None,
            "resolve_membership_delta": resolve_membership_delta if act == "resolve_group" else None,
            "before_policy": before_tier_policy.to_dict() if before_tier_policy is not None else None,
            "after_policy": proposed_tier_policy.to_dict() if proposed_tier_policy is not None else None,
            "policy_diff": policy_diff,
            "card_title": card_title,
            "card_description": card_description,
            "preserve_workspace_notice": (
                "研究工作区与历史文档完整保留，关联受管定时任务及待执行财报任务同步暂停。"
                if confirm_action_type == "watchlist_remove"
                else None
            ),
        }

    async def execute_mutation(
        self,
        user_id: str,
        *,
        action: str,
        snapshot_id: str | None = None,
        tickers: Sequence[str] | str | None = None,
        tier: str | None = None,
        policy: Mapping[str, Any] | None = None,
        expected_policy_version: int | None = None,
        expected_roster_hash: str | None = None,
        source: str = "chat",
        label: str | None = None,
        criteria: Mapping[str, Any] | None = None,
        default_tier: str | None = None,
        auto_refresh: bool | None = None,
        group_id: str | None = None,
        tags: Sequence[str] | None = None,
        override_user_pause: bool | None = None,
        prevalidated: Mapping[str, Mapping[str, Any]] | None = None,
        now: datetime | None = None,
        sync_home: bool = True,
        reconcile_now: bool = True,
        allow_internal: bool = False,
    ) -> dict[str, Any]:
        """Execute Stage 1 (locked server-snapshot check + DB write + Outbox insert) and Stage 2/3 reconciliation."""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        act = str(action or "").strip().lower()
        norm_source = normalize_source(source, default="chat")
        clean_snapshot_id = str(snapshot_id or "").strip() or None

        if act == "update_tags":
            norm_tickers = normalize_tickers_input(tickers)
            norm_tags = normalize_tags(tags)
            outbox_job_tags: dict[str, Any] | None = None
            updated_rows: list[dict[str, Any]] = []

            async with coverage_db.get_db_connection() as db_conn:
                async with db_conn.transaction():
                    await coverage_db.acquire_coverage_advisory_lock(db_conn, uid)
                    updated_rows = await coverage_db.update_coverage_tags(
                        uid, norm_tickers, norm_tags, conn=db_conn
                    )
                    new_rev_tags = await bump_user_roster_revision(uid, conn=db_conn)
                    live_policies = await get_user_tier_policies(uid, conn=db_conn)
                    outbox_job_tags = await insert_reconcile_job(
                        uid,
                        trigger_action="update_tags",
                        target_policy_versions={
                            t: p.policy_version for t, p in live_policies.items()
                        },
                        pending_tickers=[],
                        override_user_pause=False,
                        roster_revision=new_rev_tags,
                        conn=db_conn,
                    )

            reconcile_res_tags: dict[str, Any] = {}
            if reconcile_now and outbox_job_tags is not None:
                reconcile_res_tags = await self._reconciler.reconcile_job(
                    uid, str(outbox_job_tags["job_id"]), sync_home=sync_home
                )
            return {
                "ok": True,
                "action": "update_tags",
                "updated_tickers": [str(r["ticker"]) for r in updated_rows],
                "tags": norm_tags,
                "job_id": str(outbox_job_tags["job_id"]) if outbox_job_tags else None,
                "reconcile_status": (
                    reconcile_res_tags.get("status") if reconcile_res_tags else "pending"
                ),
                "home_synced": bool(reconcile_res_tags.get("yaml_synced", False)),
            }

        if act == "resolve_group":
            if not allow_internal and clean_snapshot_id is None:
                raise WatchlistScopeChangedError(
                    "刷新规则组需要预览确认快照，或由后台定期任务内部触发。"
                )
            gid = str(group_id or "").strip()
            if not gid:
                raise ValueError("user_id and group_id are required")
            group_row = await coverage_db.get_coverage_group(uid, gid)
            if group_row is None:
                raise ValueError(f"Coverage group {gid!r} not found")
            grp_label = str(group_row.get("label") or "")
            grp_criteria = group_row.get("criteria") or {}
            grp_default_tier = normalize_tier(group_row.get("default_tier"), default="watch")

            preview = await preview_coverage_group(grp_label, grp_criteria)
            resolved_companies = preview.get("companies") or []
            rejected_tickers = list(preview.get("not_found") or [])
            matched_by_ticker = {
                str(c["ticker"]).upper(): dict(c)
                for c in resolved_companies
                if c.get("ticker")
            }
            matched_set = set(matched_by_ticker.keys())
            resolved_ts = now or datetime.now(timezone.utc)
            owned_ws_resolve = await _user_workspace_tickers(uid)

            resolve_stage1: dict[str, Any] = {}
            outbox_job_resolve: dict[str, Any] | None = None
            has_any_rows = False

            async with coverage_db.get_db_connection() as db_conn:
                async with db_conn.transaction():
                    await coverage_db.acquire_coverage_advisory_lock(db_conn, uid)
                    current_group = await coverage_db.get_coverage_group(uid, gid, conn=db_conn)
                    if current_group is None:
                        return {"status": "group_deleted", "ok": False, "group_id": gid}
                    eff_default_tier = normalize_tier(
                        current_group.get("default_tier") or grp_default_tier,
                        default="watch",
                    )
                    existing_rows = await coverage_db.list_coverage_rows(uid, conn=db_conn)
                    existing_by_ticker = {
                        str(r["ticker"]).upper(): r for r in existing_rows if r.get("ticker")
                    }
                    has_any_rows = bool(matched_by_ticker or existing_rows)

                    live_delta = _compute_resolve_group_membership_delta(
                        gid, matched_set, existing_rows, existing_by_ticker
                    )
                    new_tickers = list(live_delta["new_tickers"])
                    tickers_needing_membership = list(live_delta["membership_tickers"])
                    stale_candidates = list(live_delta["stale_tickers"])
                    unstale_candidates = list(live_delta["unstale_tickers"])
                    if not allow_internal:
                        snap_row_res = await get_confirm_snapshot(
                            uid,
                            clean_snapshot_id or "",
                            for_update=True,
                            conn=db_conn,
                        )
                        if snap_row_res is None:
                            raise WatchlistScopeChangedError("确认快照无效、已过期或已被使用，请重新预览确认。")
                        snap_res = snap_row_res.get("payload")
                        if not isinstance(snap_res, Mapping):
                            raise WatchlistScopeChangedError()
                        if str(snap_res.get("raw_action") or "").strip().lower() != "resolve_group":
                            raise WatchlistScopeChangedError()
                        if str(snap_res.get("group_id") or "").strip() != gid:
                            raise WatchlistScopeChangedError()
                        live_state_res = await get_user_roster_state(uid, conn=db_conn)
                        if int(snap_res.get("roster_revision", 0)) != int(
                            live_state_res.get("roster_revision") or 0
                        ):
                            raise WatchlistScopeChangedError()
                        snap_new_tickers = sorted(
                            str(t).strip().upper()
                            for t in (snap_res.get("target_tickers") or [])
                            if str(t).strip()
                        )
                        if new_tickers != snap_new_tickers:
                            raise WatchlistScopeChangedError()
                        snap_delta_raw = snap_res.get("resolve_membership_delta")
                        if not isinstance(snap_delta_raw, Mapping):
                            raise WatchlistScopeChangedError()
                        snap_delta = _normalize_resolve_membership_delta(snap_delta_raw)
                        if snap_delta != live_delta:
                            raise WatchlistScopeChangedError()
                        consumed_res = await consume_confirm_snapshot(
                            uid, clean_snapshot_id or "", conn=db_conn
                        )
                        if consumed_res is None:
                            raise WatchlistScopeChangedError("确认快照已被使用或已过期，请重新预览确认。")

                    units_unavailable = False
                    if tickers_needing_membership:
                        existing_groups = await coverage_db.list_coverage_groups(uid, conn=db_conn)
                        valid_group_ids = {
                            str(g["group_id"]).strip()
                            for g in existing_groups
                            if g.get("group_id")
                        } | {gid}
                        rows_to_upsert = _build_upsert_rows_from_existing(
                            uid=uid,
                            valid_items=[matched_by_ticker[t] for t in tickers_needing_membership],
                            existing_rows=existing_by_ticker,
                            ws_by_ticker=owned_ws_resolve,
                            norm_tier=eff_default_tier,
                            norm_source="group",
                            group_id=gid,
                            valid_group_ids=valid_group_ids,
                        )
                        _, upserted = await coverage_db.upsert_coverage_rows(
                            uid, rows_to_upsert, conn=db_conn
                        )
                        added_companies = [row_to_company(r) for r in upserted]
                        from src.dalton.coverage.service import _overlay_canonical_unit_workspaces

                        units_unavailable = await _overlay_canonical_unit_workspaces(
                            uid, added_companies, raw_rows=upserted, conn=db_conn
                        )

                    marked_stale: list[str] = []
                    if stale_candidates:
                        marked_stale = await coverage_db.mark_coverage_rows_group_stale(
                            uid, stale_candidates, group_id=gid, stale=True, conn=db_conn
                        )
                    if unstale_candidates:
                        await coverage_db.mark_coverage_rows_group_stale(
                            uid, unstale_candidates, group_id=gid, stale=False, conn=db_conn
                        )

                    updated_group = await coverage_db.update_coverage_group_resolved(
                        uid, gid, last_resolved_at=resolved_ts, conn=db_conn
                    )
                    new_rev_resolve = await bump_user_roster_revision(uid, conn=db_conn)
                    live_policies = await get_user_tier_policies(uid, conn=db_conn)
                    outbox_job_resolve = await insert_reconcile_job(
                        uid,
                        trigger_action="resolve_group",
                        target_policy_versions={
                            t: p.policy_version for t, p in live_policies.items()
                        },
                        pending_tickers=list(new_tickers),
                        override_user_pause=False,
                        roster_revision=new_rev_resolve,
                        conn=db_conn,
                    )
                    resolve_stage1 = {
                        "ok": True,
                        "group": updated_group or group_row,
                        "matched_tickers": list(matched_by_ticker.keys()),
                        "added_tickers": new_tickers,
                        "stale_tickers": marked_stale,
                        "not_found": rejected_tickers,
                        "rejected": rejected_tickers,
                    }
                    if units_unavailable:
                        resolve_stage1["units_unavailable"] = True

            invalidate_user_aliases_cache(uid)
            if has_any_rows:
                try:
                    await ensure_user_earnings_calendar_automation(uid)
                except Exception as exc:
                    logger.debug("[WATCHLIST_MUTATION] ensure earnings calendar skipped: %s", exc)

            reconcile_res_resolve: dict[str, Any] = {}
            if reconcile_now and outbox_job_resolve is not None:
                reconcile_res_resolve = await self._reconciler.reconcile_job(
                    uid, str(outbox_job_resolve["job_id"]), sync_home=sync_home
                )
            return {
                **resolve_stage1,
                "job_id": str(outbox_job_resolve["job_id"]) if outbox_job_resolve else None,
                "reconcile_status": (
                    reconcile_res_resolve.get("status") if reconcile_res_resolve else "pending"
                ),
                "home_synced": (
                    bool(reconcile_res_resolve.get("yaml_synced", False)) if sync_home else False
                ),
            }

        if act not in (
            "add",
            "add_group",
            "set_tier",
            "remove",
            "remove_group",
            "set_tier_policy",
            "reset_tier_policy",
        ):
            raise ValueError(f"Unsupported watchlist mutation action: {action!r}")

        caller_raw_tickers = normalize_tickers_input(tickers)
        caller_tier_norm = (
            normalize_tier(default_tier if act == "add_group" and default_tier else tier, default="watch")
            if (tier is not None or (act == "add_group" and default_tier is not None))
            else None
        )
        caller_group_id = str(group_id or "").strip() or None
        caller_label = str(label or "").strip() or None

        # Pre-transaction validation ONLY for exempt `add` (no snapshot_id).
        # Confirmable actions MUST carry a server-issued `snapshot_id` whose full parameters
        # and prevalidated tickers are loaded from DB under the advisory lock.
        exempt_valid_items: list[dict[str, Any]] = []
        exempt_not_found: list[str] = []
        exempt_norm_tickers: list[str] = []
        exempt_tier: str = "watch"

        if clean_snapshot_id is None:
            if act != "add" or bool(override_user_pause):
                raise WatchlistScopeChangedError("此操作会改变观察名单或已有任务，请先预览并确认后再执行。")
            if not caller_raw_tickers:
                raise ValueError("执行 add 时必须提供 tickers。")
            if len(caller_raw_tickers) > EXEMPT_ADD_MAX_TICKERS:
                raise WatchlistScopeChangedError("单次新增超过 5 只标的，请先预览并确认后再执行。")
            exempt_tier = normalize_tier(tier, default="watch")
            if prevalidated is not None:
                exempt_valid_items = []
                exempt_not_found = []
                for sym in caller_raw_tickers:
                    if sym in prevalidated:
                        info = dict(prevalidated[sym])
                        info["ticker"] = sym
                        exempt_valid_items.append(info)
                    else:
                        exempt_not_found.append(sym)
            else:
                exempt_valid_items, exempt_not_found = await validate_and_enrich_tickers(
                    caller_raw_tickers
                )
            exempt_norm_tickers = [str(item["ticker"]).upper() for item in exempt_valid_items]
            if not exempt_norm_tickers:
                return {
                    "ok": len(exempt_not_found) == 0,
                    "added": [],
                    "added_tickers": [],
                    "not_found": exempt_not_found,
                    "tier": exempt_tier,
                    "automation_created": False,
                    "home_synced": False,
                }

        owned_ws = await _user_workspace_tickers(uid)

        # Stage 1: short atomic transaction (advisory lock -> verify/consume server snapshot -> DB write -> outbox insert)
        stage1_result: dict[str, Any] = {}
        outbox_job: dict[str, Any] | None = None

        async def _stage1_tx(conn: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
            await coverage_db.acquire_coverage_advisory_lock(conn, uid)
            live_rows = await coverage_db.list_coverage_rows(uid, conn=conn)
            live_by_ticker = {
                str(r["ticker"]).strip().upper(): r for r in live_rows if r.get("ticker")
            }
            live_policies = await get_user_tier_policies(uid, conn=conn)
            live_roster_state = await get_user_roster_state(uid, conn=conn)
            live_roster_rev = int(live_roster_state.get("roster_revision") or 0)

            live_autos: list[dict[str, Any]] | None = None

            async def _get_live_autos() -> list[dict[str, Any]]:
                nonlocal live_autos
                if live_autos is None:
                    try:
                        live_autos = list(await list_all_user_automations(uid, conn=conn))
                    except Exception:
                        live_autos = []
                return live_autos

            job_context: dict[str, Any] = {}
            eff_norm_tickers: list[str] = []
            eff_not_found: list[str] = []
            eff_tier: str | None = None
            eff_group_id: str | None = None
            eff_group_label: str = ""
            eff_criteria: dict[str, Any] = {}
            eff_auto_refresh: bool = True
            eff_override_pause: bool = False
            eff_valid_items: list[dict[str, Any]] = []
            eff_proposed_policy: dict[str, Any] | None = None
            eff_policy_diff: dict[str, Any] | None = None

            if clean_snapshot_id is not None:
                snap_row = await get_confirm_snapshot(
                    uid, clean_snapshot_id, for_update=True, conn=conn
                )
                if (
                    snap_row is None
                    or snap_row.get("consumed_at") is not None
                    or snap_row.get("is_expired")
                ):
                    raise WatchlistScopeChangedError()
                snap = snap_row.get("payload")
                if not isinstance(snap, Mapping):
                    raise WatchlistScopeChangedError()
                if str(snap.get("owner_user_id") or "").strip() != uid:
                    raise WatchlistScopeChangedError()
                if str(snap.get("raw_action") or "").strip().lower() != act:
                    raise WatchlistScopeChangedError()

                # Reject any caller-provided parameter that conflicts with the server-issued snapshot
                snap_tickers = [
                    str(t).strip().upper()
                    for t in (snap.get("target_tickers") or [])
                    if str(t).strip()
                ]
                snap_not_found = [
                    str(t).strip().upper()
                    for t in (snap.get("not_found") or [])
                    if str(t).strip()
                ]
                snap_tier_raw = snap.get("target_tier")
                snap_tier = (
                    str(snap_tier_raw).strip().lower()
                    if snap_tier_raw is not None and str(snap_tier_raw).strip()
                    else None
                )
                snap_gid = str(snap.get("group_id") or "").strip() or None
                snap_glabel = str(snap.get("group_label") or "").strip()
                snap_criteria = dict(snap.get("criteria") or {}) if snap.get("criteria") is not None else {}
                snap_auto_refresh = bool(
                    snap.get("auto_refresh", True) if snap.get("auto_refresh") is not None else True
                )
                snap_override = bool(snap.get("override_user_pause", False))

                if caller_tier_norm is not None and caller_tier_norm != snap_tier:
                    raise WatchlistScopeChangedError()
                if override_user_pause is not None and bool(override_user_pause) != snap_override:
                    raise WatchlistScopeChangedError()
                if act in ("add", "set_tier", "remove") and caller_raw_tickers:
                    allowed_req = set(snap_tickers) | set(snap_not_found)
                    if not set(caller_raw_tickers).issubset(allowed_req):
                        raise WatchlistScopeChangedError()
                    if act == "add" and sorted(set(caller_raw_tickers) - set(snap_not_found)) != sorted(
                        set(snap_tickers)
                    ):
                        raise WatchlistScopeChangedError()
                    if act in ("set_tier", "remove") and sorted(
                        t for t in caller_raw_tickers if t in live_by_ticker
                    ) != sorted(set(snap_tickers)):
                        raise WatchlistScopeChangedError()
                if act == "add_group":
                    if caller_label is not None and caller_label != snap_glabel:
                        raise WatchlistScopeChangedError()
                    if criteria is not None and dict(criteria) != snap_criteria:
                        raise WatchlistScopeChangedError()
                    if auto_refresh is not None and bool(auto_refresh) != snap_auto_refresh:
                        raise WatchlistScopeChangedError()
                if act == "remove_group":
                    if not snap_gid:
                        raise WatchlistScopeChangedError()
                    if caller_group_id and caller_group_id != snap_gid:
                        if caller_group_id.lower() != snap_glabel.lower():
                            raise WatchlistScopeChangedError()
                    if caller_label and caller_label.lower() != snap_glabel.lower():
                        raise WatchlistScopeChangedError()
                if act in ("set_tier_policy", "reset_tier_policy"):
                    if snap_tier not in VALID_TIERS:
                        raise WatchlistScopeChangedError()
                    snap_prop_pol = snap.get("proposed_policy")
                    if not isinstance(snap_prop_pol, Mapping):
                        raise WatchlistScopeChangedError()
                    if expected_policy_version is not None:
                        snap_exp_pv = snap.get("expected_policy_version")
                        if snap_exp_pv is not None and int(expected_policy_version) != int(snap_exp_pv):
                            raise WatchlistScopeChangedError()
                    if expected_roster_hash is not None and str(expected_roster_hash).strip():
                        if str(expected_roster_hash).strip() != str(snap.get("expected_roster_hash") or ""):
                            raise WatchlistScopeChangedError()
                    if policy is not None:
                        if not isinstance(policy, Mapping):
                            raise WatchlistScopeChangedError()
                        for pk, pv in policy.items():
                            if pk in TIER_POLICY_MUTABLE_FIELDS and pv is not None:
                                if pk == "earnings_stages":
                                    if list(pv) != list(snap_prop_pol.get(pk) or []):
                                        raise WatchlistScopeChangedError()
                                elif pk == "move_threshold_pct":
                                    if round(float(pv), 2) != round(float(snap_prop_pol.get(pk) or 0), 2):
                                        raise WatchlistScopeChangedError()
                                elif pv != snap_prop_pol.get(pk):
                                    raise WatchlistScopeChangedError()

                # Check roster revision, policy versions, roster+owner hash, group deletion set, pause versions
                if int(snap.get("roster_revision", 0)) != live_roster_rev:
                    raise WatchlistScopeChangedError()

                exp_versions = snap.get("expected_policy_versions")
                if not isinstance(exp_versions, Mapping):
                    raise WatchlistScopeChangedError()
                for t_name, exp_ver in exp_versions.items():
                    live_ver = live_policies.get(
                        str(t_name).lower(), get_default_tier_policy(str(t_name).lower())
                    ).policy_version
                    if int(live_ver) != int(exp_ver):
                        raise WatchlistScopeChangedError()

                exp_hash = snap.get("expected_roster_hash")
                if not isinstance(exp_hash, str) or not exp_hash:
                    raise WatchlistScopeChangedError()
                if act in ("set_tier_policy", "reset_tier_policy"):
                    live_tier_tickers = sorted(
                        sym
                        for sym, row in live_by_ticker.items()
                        if str(row.get("tier") or "").strip().lower() == snap_tier
                    )
                    if live_tier_tickers != sorted(snap_tickers):
                        raise WatchlistScopeChangedError()
                live_ws_map, live_ws_owners = await _resolve_ws_map_and_owners(
                    uid, snap_tickers, live_by_ticker, owned_ws=owned_ws, conn=conn
                )
                live_hash_with_owner = compute_roster_hash(
                    snap_tickers,
                    live_by_ticker,
                    live_ws_map,
                    ws_owners=live_ws_owners,
                )
                if live_hash_with_owner != exp_hash:
                    raise WatchlistScopeChangedError()

                if act == "remove_group":
                    assert snap_gid is not None
                    live_groups = await coverage_db.list_coverage_groups(uid, conn=conn)
                    matched_live_grp = next(
                        (
                            g
                            for g in live_groups
                            if str(g.get("group_id") or "").strip() == snap_gid
                        ),
                        None,
                    )
                    if matched_live_grp is None:
                        raise WatchlistScopeChangedError()
                    live_del_tickers = coverage_db.compute_exclusive_group_deletion_tickers(
                        snap_gid, live_groups, live_rows
                    )
                    if sorted(live_del_tickers) != sorted(snap_tickers):
                        raise WatchlistScopeChangedError()

                if snap_override:
                    autos_now = await _get_live_autos()
                    live_pause_versions: dict[str, int] = {}
                    for sym in snap_tickers:
                        ws_id = live_ws_map.get(sym)
                        if not ws_id:
                            continue
                        c_rows = _company_rows_for_workspace(
                            autos_now, workspace_id=ws_id, ticker=sym
                        )
                        live_pause_versions.update(
                            _collect_user_pause_versions(c_rows, ticker=sym)
                        )
                    raw_exp_pv = snap.get("expected_pause_versions")
                    if not isinstance(raw_exp_pv, Mapping):
                        raise WatchlistScopeChangedError()
                    norm_exp_pv = {str(k): int(v) for k, v in raw_exp_pv.items()}
                    if norm_exp_pv != live_pause_versions:
                        raise WatchlistScopeChangedError()
                    job_context["override_pause_versions"] = live_pause_versions

                consumed = await consume_confirm_snapshot(uid, clean_snapshot_id, conn=conn)
                if not consumed:
                    raise WatchlistScopeChangedError()

                eff_norm_tickers = list(snap_tickers)
                eff_not_found = list(snap_not_found)
                eff_tier = snap_tier
                eff_group_id = snap_gid
                eff_group_label = snap_glabel
                eff_criteria = snap_criteria
                eff_auto_refresh = snap_auto_refresh
                eff_override_pause = snap_override
                eff_proposed_policy = (
                    dict(snap["proposed_policy"])
                    if isinstance(snap.get("proposed_policy"), Mapping)
                    else None
                )
                eff_policy_diff = (
                    dict(snap["policy_diff"])
                    if isinstance(snap.get("policy_diff"), Mapping)
                    else None
                )
                snap_preval = snap.get("prevalidated")
                if isinstance(snap_preval, Mapping):
                    for sym in eff_norm_tickers:
                        info = dict(snap_preval.get(sym) or {"ticker": sym, "name": sym})
                        info["ticker"] = sym
                        eff_valid_items.append(info)
                elif act in ("add", "add_group"):
                    eff_valid_items = [{"ticker": sym, "name": sym} for sym in eff_norm_tickers]

            else:
                # Server-verified exempt `add`: <= 5 requested, ALL net-new, no pause override, no paused task resumed
                assert act == "add"
                if any(sym in live_by_ticker for sym in caller_raw_tickers):
                    raise WatchlistScopeChangedError("标的已在观察名单中，请先预览并确认后再修改。")
                live_ws_check, _ = await _resolve_ws_map_and_owners(
                    uid, exempt_norm_tickers, live_by_ticker, owned_ws=owned_ws, conn=conn
                )
                if any(live_ws_check.values()):
                    autos_now = await _get_live_autos()
                    eff_pol = resolve_effective_company_policy(
                        live_policies.get(
                            exempt_tier,
                            get_default_tier_policy(exempt_tier),
                        ),
                        None,
                    )
                    for sym, ws_id in live_ws_check.items():
                        if not ws_id:
                            continue
                        c_rows = _company_rows_for_workspace(
                            autos_now, workspace_id=ws_id, ticker=sym
                        )
                        _, c_counts = _estimate_workspace_task_changes(
                            ticker=sym,
                            workspace_id=ws_id,
                            old_tier=None,
                            new_tier=exempt_tier,
                            new_policy=eff_pol,
                            company_rows=c_rows,
                            override_user_pause=False,
                        )
                        if c_counts["resume_count"] > 0:
                            raise WatchlistScopeChangedError("此操作会恢复已暂停的任务，请先预览并确认后再执行。")

                eff_norm_tickers = list(exempt_norm_tickers)
                eff_not_found = list(exempt_not_found)
                eff_tier = exempt_tier
                eff_valid_items = list(exempt_valid_items)
                eff_override_pause = False

            target_versions = {
                t: p.policy_version for t, p in live_policies.items()
            }
            pending_reconcile_tickers: list[str] = []
            res_payload: dict[str, Any] = {}

            if act == "add":
                assert eff_tier is not None
                existing_groups = await coverage_db.list_coverage_groups(uid, conn=conn)
                valid_group_ids = {
                    str(g["group_id"]).strip()
                    for g in existing_groups
                    if g.get("group_id")
                }
                rows_to_upsert = _build_upsert_rows_from_existing(
                    uid=uid,
                    valid_items=eff_valid_items,
                    existing_rows=live_by_ticker,
                    ws_by_ticker=owned_ws,
                    norm_tier=eff_tier,
                    norm_source=norm_source,
                    group_id=None,
                    valid_group_ids=valid_group_ids,
                )
                _, upserted = await coverage_db.upsert_coverage_rows(
                    uid, rows_to_upsert, conn=conn
                )
                added_companies = [row_to_company(r) for r in upserted]
                added_tickers = [str(c["ticker"]) for c in added_companies]
                pending_reconcile_tickers = list(added_tickers)
                res_payload = {
                    "ok": True,
                    "added": added_companies,
                    "added_tickers": added_tickers,
                    "not_found": eff_not_found,
                    "tier": eff_tier,
                }

            elif act == "add_group":
                assert eff_tier is not None
                now_utc = datetime.now(timezone.utc)
                group_row = await coverage_db.insert_coverage_group(
                    uid,
                    label=eff_group_label,
                    criteria=eff_criteria,
                    default_tier=eff_tier,
                    auto_refresh=bool(eff_auto_refresh),
                    last_resolved_at=now_utc,
                    conn=conn,
                )
                new_gid = str(group_row["group_id"])
                existing_groups = await coverage_db.list_coverage_groups(uid, conn=conn)
                valid_group_ids = {
                    str(g["group_id"]).strip()
                    for g in existing_groups
                    if g.get("group_id")
                } | {new_gid}
                rows_to_upsert = _build_upsert_rows_from_existing(
                    uid=uid,
                    valid_items=eff_valid_items,
                    existing_rows=live_by_ticker,
                    ws_by_ticker=owned_ws,
                    norm_tier=eff_tier,
                    norm_source="group",
                    group_id=new_gid,
                    valid_group_ids=valid_group_ids,
                )
                _, upserted = await coverage_db.upsert_coverage_rows(
                    uid, rows_to_upsert, conn=conn
                )
                added_tickers = [str(r["ticker"]) for r in upserted]
                added_companies = [row_to_company(r) for r in upserted]
                from src.dalton.coverage.service import _overlay_canonical_unit_workspaces

                units_unavailable = await _overlay_canonical_unit_workspaces(
                    uid, added_companies, raw_rows=upserted, conn=conn
                )
                pending_reconcile_tickers = [
                    t for t in added_tickers if t not in live_by_ticker
                ]
                res_payload = {
                    "ok": True,
                    "group": group_row,
                    "resolved_count": len(eff_valid_items),
                    "added_tickers": added_tickers,
                    "not_found": eff_not_found,
                    "rejected": eff_not_found,
                }
                if units_unavailable:
                    res_payload["units_unavailable"] = True

            elif act == "set_tier":
                assert eff_tier is not None
                updated_rows = await coverage_db.update_coverage_tier(
                    uid, eff_norm_tickers, eff_tier, conn=conn
                )
                updated_tickers = [str(r["ticker"]) for r in updated_rows]
                updated_set = set(updated_tickers)
                missing_tickers = list(
                    dict.fromkeys(
                        [
                            *[t for t in eff_not_found if t not in updated_set],
                            *[t for t in eff_norm_tickers if t not in updated_set],
                        ]
                    )
                )
                pending_reconcile_tickers = list(updated_tickers)
                res_payload = {
                    "ok": True,
                    "updated": updated_tickers,
                    "updated_tickers": updated_tickers,
                    "missing_tickers": missing_tickers,
                    "not_found": missing_tickers,
                    "tier": eff_tier,
                }

            elif act == "remove":
                removed_ws_map: dict[str, str] = {}
                for sym in eff_norm_tickers:
                    if sym not in live_by_ticker:
                        continue
                    ws_id = await resolve_company_workspace_id(
                        uid, sym, cov_row=live_by_ticker.get(sym), conn=conn
                    )
                    if not ws_id:
                        ws_id = owned_ws.get(sym)
                    if ws_id:
                        removed_ws_map[sym] = ws_id

                removed = await coverage_db.delete_coverage_tickers(
                    uid, eff_norm_tickers, conn=conn
                )
                removed_set = set(removed)
                if removed_ws_map:
                    job_context["removed_workspaces"] = {
                        k: v for k, v in removed_ws_map.items() if k in removed_set
                    }
                missing_tickers = list(
                    dict.fromkeys(
                        [
                            *[t for t in eff_not_found if t not in removed_set],
                            *[t for t in eff_norm_tickers if t not in removed_set],
                        ]
                    )
                )
                preserved_ws = [t for t in removed if t in removed_ws_map or t in owned_ws]
                notice = (
                    f"{', '.join(preserved_ws)} 已有公司 workspace，workspace 和历史文档不会被删除，关联受管自动任务将同步暂停。"
                    if preserved_ws
                    else None
                )
                pending_reconcile_tickers = list(removed)
                res_payload = {
                    "ok": True,
                    "removed": removed,
                    "removed_tickers": removed,
                    "not_found": missing_tickers,
                    "workspaces_preserved": preserved_ws,
                    "notice": notice,
                    "workspace_notice": notice,
                }

            elif act == "remove_group":
                assert eff_group_id is not None
                live_groups_now = await coverage_db.list_coverage_groups(uid, conn=conn)
                expected_del_syms = coverage_db.compute_exclusive_group_deletion_tickers(
                    eff_group_id, live_groups_now, live_rows
                )
                removed_ws_map = {}
                for sym in expected_del_syms:
                    ws_id = await resolve_company_workspace_id(
                        uid, sym, cov_row=live_by_ticker.get(sym), conn=conn
                    )
                    if not ws_id:
                        ws_id = owned_ws.get(sym)
                    if ws_id:
                        removed_ws_map[sym] = ws_id

                del_res = await coverage_db.delete_coverage_group_and_exclusive_members(
                    uid, eff_group_id, conn=conn
                )
                removed = list(del_res.get("removed_tickers") or [])
                removed_set = set(removed)
                if removed_ws_map:
                    job_context["removed_workspaces"] = {
                        k: v for k, v in removed_ws_map.items() if k in removed_set
                    }
                pending_reconcile_tickers = list(removed)
                res_payload = {
                    "ok": bool(del_res.get("deleted")),
                    **del_res,
                }

            elif act in ("set_tier_policy", "reset_tier_policy"):
                assert eff_tier is not None and eff_proposed_policy is not None
                before_pol = live_policies.get(eff_tier, get_default_tier_policy(eff_tier))
                policy_patch = extract_tier_policy_patch(eff_proposed_policy)
                updated_pol = await upsert_tier_policy(
                    uid,
                    eff_tier,
                    policy=policy_patch,
                    conn=conn,
                )
                target_versions[eff_tier] = updated_pol.policy_version
                pending_reconcile_tickers = list(eff_norm_tickers)
                res_payload = {
                    "ok": True,
                    "action": act,
                    "tier": eff_tier,
                    "policy": updated_pol.to_dict(),
                    "before_policy": before_pol.to_dict(),
                    "policy_diff": eff_policy_diff or diff_tier_policies(before_pol, updated_pol),
                    "affected_tickers": list(eff_norm_tickers),
                }

            # Bump monotonic roster revision & insert outbox record in the SAME transaction
            new_rev = await bump_user_roster_revision(uid, conn=conn)
            job_row = await insert_reconcile_job(
                uid,
                trigger_action=act,
                target_policy_versions=target_versions,
                pending_tickers=pending_reconcile_tickers,
                override_user_pause=eff_override_pause,
                job_context=job_context,
                roster_revision=new_rev,
                conn=conn,
            )
            return res_payload, job_row

        async with coverage_db.get_db_connection() as db_conn:
            async with db_conn.transaction():
                stage1_result, outbox_job = await _stage1_tx(db_conn)

        invalidate_user_aliases_cache(uid)

        # Ensure personal earnings calendar automation exists whenever roster is non-empty
        automation_res: dict[str, Any] = {}
        try:
            if await coverage_db.count_coverage_rows(uid) > 0:
                automation_res = await ensure_user_earnings_calendar_automation(uid)
        except Exception as exc:
            logger.debug("[WATCHLIST_MUTATION] ensure earnings calendar skipped: %s", exc)

        # Stage 2 & 3: Reconcile automations per company + sync Home YAML
        reconcile_res: dict[str, Any] = {}
        if reconcile_now and outbox_job is not None:
            reconcile_res = await self._reconciler.reconcile_job(
                uid, str(outbox_job["job_id"]), sync_home=sync_home
            )

        out: dict[str, Any] = {
            **stage1_result,
            "job_id": str(outbox_job["job_id"]) if outbox_job else None,
            "reconcile_status": reconcile_res.get("status") if reconcile_res else "pending",
            "failed_items": reconcile_res.get("failed_items") if reconcile_res else [],
            "home_synced": bool(reconcile_res.get("yaml_synced", False)),
            "automation": automation_res or None,
            "automation_created": automation_res.get("action") == "created",
        }
        return out


__all__ = [
    "EXEMPT_ADD_MAX_TICKERS",
    "TIER_DISPLAY_ZH",
    "WATCHLIST_CONFIRM_ACTION_TYPES",
    "WatchlistMutationService",
    "WatchlistScopeChangedError",
    "compute_roster_hash",
    "normalize_tickers_input",
]
