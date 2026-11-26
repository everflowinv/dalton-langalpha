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
from src.dalton.watchlist.policies import (
    TierPolicy,
    get_default_tier_policy,
    get_user_tier_policies,
    insert_reconcile_job,
    resolve_effective_company_policy,
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
            old_cron = existing.get("cron_expression")
            new_cron = spec.get("cron_expression")
            if old_cron != new_cron or old_tier != new_tier:
                counts["update_count"] += 1
                changes.append(
                    {
                        "name": s_name,
                        "change": "update",
                        "old_cron": old_cron,
                        "new_cron": new_cron,
                    }
                )

    # Preview future earnings stage pauses when earnings_stages shrinks or empties
    allowed_stages = set(new_policy.earnings_stages)
    for r in company_rows:
        if not _is_future_earn_once(r):
            continue
        r_name = str(r.get("name") or "").strip().lower()
        stage = _earn_stage_from_name(r_name, ticker)
        if not allowed_stages or (stage is not None and stage not in allowed_stages):
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
        label: str | None = None,
        criteria: Mapping[str, Any] | None = None,
        default_tier: str | None = None,
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

        # Resolve target tickers & tier according to action
        raw_tickers: list[str] = []
        norm_tickers: list[str] = []
        not_found: list[str] = []
        prevalidated: dict[str, dict[str, Any]] = {}
        target_tier: str | None = None
        resolved_group_id: str | None = None
        resolved_group_label: str | None = None
        resolved_criteria: dict[str, Any] = dict(criteria or {})

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
                        policies.get(eff_new_tier, get_default_tier_policy(eff_new_tier)),
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
        elif act == "add_group":
            confirm_action_type = "watchlist_batch_add"
            requires_confirmation = True
        elif act == "set_tier":
            if norm_tickers or override_user_pause:
                confirm_action_type = "watchlist_set_tier"
                requires_confirmation = True
        elif act in ("remove", "remove_group"):
            confirm_action_type = "watchlist_remove"
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
        else:
            card_title = ""
            card_description = ""

        return {
            "ok": True,
            "action": confirm_action_type,
            "raw_action": act,
            "tool_call_id": tool_call_id,
            "requires_confirmation": requires_confirmation,
            "owner_user_id": uid,
            "requested_count": requested_count,
            "target_tickers": norm_tickers,
            "target_tier": target_tier,
            "not_found": not_found,
            "expected_roster_hash": expected_roster_hash,
            "expected_policy_versions": expected_policy_versions,
            "expected_pause_versions": expected_pause_versions,
            "affected_tickers": affected_tickers,
            "affected_workspaces": affected_workspaces,
            "task_summary": task_summary,
            "override_user_pause": bool(override_user_pause),
            "group_id": resolved_group_id,
            "group_label": resolved_group_label,
            "criteria": resolved_criteria if act == "add_group" else None,
            "prevalidated": prevalidated if act in ("add", "add_group") else None,
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
        tickers: Sequence[str] | str | None = None,
        tier: str | None = None,
        source: str = "chat",
        label: str | None = None,
        criteria: Mapping[str, Any] | None = None,
        default_tier: str | None = None,
        auto_refresh: bool = True,
        group_id: str | None = None,
        tags: Sequence[str] | None = None,
        override_user_pause: bool = False,
        expected_snapshot: Mapping[str, Any] | None = None,
        prevalidated: Mapping[str, Mapping[str, Any]] | None = None,
        require_snapshot: bool = False,
        sync_home: bool = True,
        reconcile_now: bool = True,
    ) -> dict[str, Any]:
        """Execute Stage 1 (locked check + DB write + Outbox insert) and Stage 2/3 reconciliation."""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        act = str(action or "").strip().lower()
        norm_source = normalize_source(source, default="chat")

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
                    live_policies = await get_user_tier_policies(uid, conn=db_conn)
                    outbox_job_tags = await insert_reconcile_job(
                        uid,
                        trigger_action="update_tags",
                        target_policy_versions={
                            t: p.policy_version for t, p in live_policies.items()
                        },
                        pending_tickers=[],
                        override_user_pause=False,
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

        # Pre-transaction external lookups (never hold DB advisory lock across yfinance calls)
        snap_tickers = (
            [
                str(t).strip().upper()
                for t in (expected_snapshot.get("target_tickers") or [])
                if str(t).strip()
            ]
            if isinstance(expected_snapshot, Mapping)
            and expected_snapshot.get("target_tickers") is not None
            else None
        )
        snap_prevalidated = (
            expected_snapshot.get("prevalidated")
            if isinstance(expected_snapshot, Mapping)
            and isinstance(expected_snapshot.get("prevalidated"), Mapping)
            else None
        )
        eff_prevalidated: Mapping[str, Mapping[str, Any]] | None = prevalidated or snap_prevalidated

        resolved_valid_items: list[dict[str, Any]] = []
        snap_not_found: list[str] = [
            str(t).strip().upper()
            for t in ((expected_snapshot or {}).get("not_found") or [])
            if str(t).strip()
        ]
        not_found: list[str] = []
        norm_tickers: list[str] = []
        norm_tier: str | None = None
        caller_raw_tickers = normalize_tickers_input(tickers)
        clean_label = str(label or (expected_snapshot or {}).get("group_label") or "").strip()
        eff_criteria = dict(
            criteria
            if criteria is not None
            else ((expected_snapshot or {}).get("criteria") or {})
        )
        snap_group_id = (
            str(expected_snapshot.get("group_id") or "").strip() or None
            if isinstance(expected_snapshot, Mapping)
            else None
        )
        raw_group_id = str(group_id or "").strip() or None
        # When previewed by label, execution MUST use the preview-resolved UUID (Item 3)
        eff_group_id = snap_group_id or raw_group_id

        if act == "add":
            norm_tier = normalize_tier(
                tier or (expected_snapshot or {}).get("target_tier"), default="watch"
            )
            raw_tickers = (
                list(caller_raw_tickers)
                if caller_raw_tickers
                else (list(snap_tickers) if snap_tickers is not None else [])
            )
            if not raw_tickers:
                raise ValueError("执行 add 时必须提供 tickers。")
            if eff_prevalidated is not None:
                for sym in raw_tickers:
                    if sym in eff_prevalidated:
                        info = dict(eff_prevalidated[sym])
                        info["ticker"] = sym
                        resolved_valid_items.append(info)
                        norm_tickers.append(sym)
                    else:
                        not_found.append(sym)
                for sym in snap_not_found:
                    if sym not in not_found and sym not in norm_tickers:
                        not_found.append(sym)
            else:
                resolved_valid_items, not_found = await validate_and_enrich_tickers(raw_tickers)
                norm_tickers = [str(item["ticker"]).upper() for item in resolved_valid_items]
            if not norm_tickers:
                return {
                    "ok": len(not_found) == 0,
                    "added": [],
                    "added_tickers": [],
                    "not_found": not_found,
                    "tier": norm_tier,
                    "automation_created": False,
                    "home_synced": False,
                }

        elif act == "add_group":
            if not clean_label:
                raise ValueError("执行 add_group 时必须提供 label。")
            norm_tier = normalize_tier(
                default_tier or tier or (expected_snapshot or {}).get("target_tier"),
                default="watch",
            )
            if eff_prevalidated is not None:
                norm_tickers = list(eff_prevalidated.keys())
                resolved_valid_items = [
                    {**dict(v), "ticker": k} for k, v in eff_prevalidated.items()
                ]
                not_found = list((expected_snapshot or {}).get("not_found") or [])
            else:
                grp_prev = await preview_coverage_group(clean_label, eff_criteria)
                resolved_valid_items = [
                    dict(c) for c in (grp_prev.get("companies") or []) if c.get("ticker")
                ]
                norm_tickers = [str(c["ticker"]).upper() for c in resolved_valid_items]
                not_found = list(grp_prev.get("not_found") or [])

        elif act == "set_tier":
            norm_tier = normalize_tier(
                tier or (expected_snapshot or {}).get("target_tier"), default="watch"
            )
            norm_tickers = (
                list(caller_raw_tickers)
                if caller_raw_tickers
                else (list(snap_tickers) if snap_tickers is not None else [])
            )
            if not norm_tickers:
                raise ValueError("执行 set_tier 时必须提供 tickers。")

        elif act == "remove":
            norm_tickers = (
                list(caller_raw_tickers)
                if caller_raw_tickers
                else (list(snap_tickers) if snap_tickers is not None else [])
            )
            if not norm_tickers:
                raise ValueError("执行 remove 时必须提供 tickers。")

        elif act == "remove_group":
            if not eff_group_id and clean_label:
                groups = await coverage_db.list_coverage_groups(uid)
                matched = next(
                    (
                        g
                        for g in groups
                        if str(g.get("label") or "").strip().lower() == clean_label.lower()
                    ),
                    None,
                )
                eff_group_id = str(matched["group_id"]) if matched else clean_label
            if not eff_group_id:
                raise ValueError("执行 remove_group 时必须提供 group_id。")
            norm_tickers = list(snap_tickers or [])

        else:
            raise ValueError(f"Unsupported watchlist mutation action: {action!r}")

        owned_ws = await _user_workspace_tickers(uid)
        requested_count_for_exempt = len(caller_raw_tickers) if caller_raw_tickers else len(norm_tickers)

        # Stage 1: short atomic transaction (advisory lock -> verify snapshot -> DB write -> outbox insert)
        stage1_result: dict[str, Any] = {}
        outbox_job: dict[str, Any] | None = None

        async def _stage1_tx(conn: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
            await coverage_db.acquire_coverage_advisory_lock(conn, uid)
            live_rows = await coverage_db.list_coverage_rows(uid, conn=conn)
            live_by_ticker = {
                str(r["ticker"]).strip().upper(): r for r in live_rows if r.get("ticker")
            }
            live_policies = await get_user_tier_policies(uid, conn=conn)

            # Determine whether this mutation requires a confirmation snapshot (Items 2 & 4)
            live_autos: list[dict[str, Any]] | None = None

            async def _get_live_autos() -> list[dict[str, Any]]:
                nonlocal live_autos
                if live_autos is None:
                    try:
                        live_autos = list(await list_all_user_automations(uid, conn=conn))
                    except Exception:
                        live_autos = []
                return live_autos

            is_confirmable = act in ("remove", "set_tier", "remove_group", "add_group") or bool(
                override_user_pause
            )
            if not is_confirmable and act == "add":
                req_syms = caller_raw_tickers or norm_tickers
                if requested_count_for_exempt > EXEMPT_ADD_MAX_TICKERS or any(
                    sym in live_by_ticker for sym in req_syms
                ):
                    is_confirmable = True
                else:
                    # Also require confirmation if adding any ticker would resume a paused task
                    live_ws_check, _ = await _resolve_ws_map_and_owners(
                        uid, norm_tickers, live_by_ticker, owned_ws=owned_ws, conn=conn
                    )
                    if any(live_ws_check.values()):
                        autos_now = await _get_live_autos()
                        eff_pol = resolve_effective_company_policy(
                            live_policies.get(
                                norm_tier or "watch",
                                get_default_tier_policy(norm_tier or "watch"),
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
                                new_tier=norm_tier,
                                new_policy=eff_pol,
                                company_rows=c_rows,
                                override_user_pause=False,
                            )
                            if c_counts["resume_count"] > 0:
                                is_confirmable = True
                                break

            has_snapshot = isinstance(expected_snapshot, Mapping) and bool(expected_snapshot)
            if require_snapshot and is_confirmable and not has_snapshot:
                raise WatchlistScopeChangedError("此操作会改变观察名单或已有任务，请先预览并确认后再执行。")

            job_context: dict[str, Any] = {}

            # Snapshot verification BEFORE any write (§5.3 Stage 1, Items 2, 3, 8)
            if has_snapshot:
                assert isinstance(expected_snapshot, Mapping)
                snap_owner = expected_snapshot.get("owner_user_id")
                if snap_owner is not None and str(snap_owner).strip() != uid:
                    raise WatchlistScopeChangedError()

                # Bind action
                snap_action = str(
                    expected_snapshot.get("raw_action")
                    or expected_snapshot.get("expected_action")
                    or ""
                ).strip().lower()
                if snap_action:
                    if snap_action != act:
                        raise WatchlistScopeChangedError()
                elif require_snapshot and is_confirmable:
                    raise WatchlistScopeChangedError()

                # Bind target_tier
                if "target_tier" in expected_snapshot or "expected_target_tier" in expected_snapshot:
                    raw_exp_tier = (
                        expected_snapshot.get("target_tier")
                        if "target_tier" in expected_snapshot
                        else expected_snapshot.get("expected_target_tier")
                    )
                    exp_tier = (
                        str(raw_exp_tier).strip().lower()
                        if raw_exp_tier is not None and str(raw_exp_tier).strip()
                        else None
                    )
                    if exp_tier != norm_tier:
                        raise WatchlistScopeChangedError()

                # Bind override_user_pause
                if (
                    "override_user_pause" in expected_snapshot
                    or "expected_override_user_pause" in expected_snapshot
                ):
                    raw_exp_override = (
                        expected_snapshot.get("override_user_pause")
                        if "override_user_pause" in expected_snapshot
                        else expected_snapshot.get("expected_override_user_pause")
                    )
                    if bool(raw_exp_override) != bool(override_user_pause):
                        raise WatchlistScopeChangedError()

                # Bind target_tickers against request tickers (accounting for snap_not_found on set_tier/remove)
                if snap_tickers is not None and act in ("add", "set_tier", "remove", "add_group"):
                    if act in ("set_tier", "remove"):
                        allowed_req = set(snap_tickers) | set(snap_not_found)
                        live_matched = sorted({t for t in norm_tickers if t in live_by_ticker})
                        if (
                            not set(norm_tickers).issubset(allowed_req)
                            or live_matched != sorted(set(snap_tickers))
                        ):
                            raise WatchlistScopeChangedError()
                    elif sorted(set(norm_tickers)) != sorted(set(snap_tickers)):
                        raise WatchlistScopeChangedError()
                elif require_snapshot and is_confirmable and act != "remove_group":
                    raise WatchlistScopeChangedError()

                # Check policy_versions for all expected tiers
                exp_versions = expected_snapshot.get("expected_policy_versions")
                if isinstance(exp_versions, Mapping):
                    for t_name, exp_ver in exp_versions.items():
                        live_ver = live_policies.get(
                            str(t_name).lower(), get_default_tier_policy(str(t_name).lower())
                        ).policy_version
                        if int(live_ver) != int(exp_ver):
                            raise WatchlistScopeChangedError()
                elif require_snapshot and is_confirmable:
                    raise WatchlistScopeChangedError()

                # Check target tickers' current roster & workspace ownership fingerprint
                exp_hash = expected_snapshot.get("expected_roster_hash")
                if isinstance(exp_hash, str) and exp_hash:
                    check_tickers = list(snap_tickers if snap_tickers is not None else norm_tickers)
                    live_ws_map, live_ws_owners = await _resolve_ws_map_and_owners(
                        uid, check_tickers, live_by_ticker, owned_ws=owned_ws, conn=conn
                    )
                    live_hash_with_owner = compute_roster_hash(
                        check_tickers,
                        live_by_ticker,
                        live_ws_map,
                        ws_owners=live_ws_owners,
                    )
                    if live_hash_with_owner != exp_hash:
                        # Backward compatibility for callers/tests computing hash without ws_owners
                        live_hash_legacy = compute_roster_hash(
                            check_tickers, live_by_ticker, live_ws_map
                        )
                        if live_hash_legacy != exp_hash:
                            raise WatchlistScopeChangedError()
                elif require_snapshot and is_confirmable:
                    raise WatchlistScopeChangedError()

                # For remove_group, verify group exists & exclusive deletion set matches snapshot (Item 3)
                if act == "remove_group":
                    if not eff_group_id:
                        raise WatchlistScopeChangedError()
                    live_groups = await coverage_db.list_coverage_groups(uid, conn=conn)
                    matched_live_grp = next(
                        (
                            g
                            for g in live_groups
                            if str(g.get("group_id") or "").strip() == eff_group_id
                        ),
                        None,
                    )
                    if matched_live_grp is None:
                        raise WatchlistScopeChangedError()
                    # If caller also supplied raw_group_id, it must match either the UUID or label of matched_live_grp
                    if raw_group_id and raw_group_id != eff_group_id:
                        grp_lbl = str(matched_live_grp.get("label") or "").strip().lower()
                        if raw_group_id.lower() != grp_lbl:
                            raise WatchlistScopeChangedError()
                    live_del_tickers = coverage_db.compute_exclusive_group_deletion_tickers(
                        eff_group_id, live_groups, live_rows
                    )
                    if snap_tickers is not None and sorted(live_del_tickers) != sorted(snap_tickers):
                        raise WatchlistScopeChangedError()

            # Bind override_user_pause to exact config_version at confirmation time (Item 8)
            if override_user_pause:
                autos_now = await _get_live_autos()
                check_syms = list(snap_tickers if snap_tickers is not None else norm_tickers)
                live_ws_map_pv, _ = await _resolve_ws_map_and_owners(
                    uid, check_syms, live_by_ticker, owned_ws=owned_ws, conn=conn
                )
                live_pause_versions: dict[str, int] = {}
                for sym in check_syms:
                    ws_id = live_ws_map_pv.get(sym)
                    if not ws_id:
                        continue
                    c_rows = _company_rows_for_workspace(
                        autos_now, workspace_id=ws_id, ticker=sym
                    )
                    live_pause_versions.update(
                        _collect_user_pause_versions(c_rows, ticker=sym)
                    )
                if has_snapshot:
                    assert isinstance(expected_snapshot, Mapping)
                    raw_exp_pv = expected_snapshot.get("expected_pause_versions")
                    if isinstance(raw_exp_pv, Mapping):
                        norm_exp_pv = {str(k): int(v) for k, v in raw_exp_pv.items()}
                        if norm_exp_pv != live_pause_versions:
                            raise WatchlistScopeChangedError()
                    elif require_snapshot:
                        raise WatchlistScopeChangedError()
                job_context["override_pause_versions"] = live_pause_versions

            target_versions = {
                t: p.policy_version for t, p in live_policies.items()
            }
            pending_reconcile_tickers: list[str] = []
            res_payload: dict[str, Any] = {}

            if act == "add":
                assert norm_tier is not None
                existing_groups = await coverage_db.list_coverage_groups(uid, conn=conn)
                valid_group_ids = {
                    str(g["group_id"]).strip()
                    for g in existing_groups
                    if g.get("group_id")
                }
                rows_to_upsert = _build_upsert_rows_from_existing(
                    uid=uid,
                    valid_items=resolved_valid_items,
                    existing_rows=live_by_ticker,
                    ws_by_ticker=owned_ws,
                    norm_tier=norm_tier,
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
                    "not_found": not_found,
                    "tier": norm_tier,
                }

            elif act == "add_group":
                assert norm_tier is not None
                now_utc = datetime.now(timezone.utc)
                group_row = await coverage_db.insert_coverage_group(
                    uid,
                    label=clean_label,
                    criteria=eff_criteria,
                    default_tier=norm_tier,
                    auto_refresh=bool(auto_refresh),
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
                    valid_items=resolved_valid_items,
                    existing_rows=live_by_ticker,
                    ws_by_ticker=owned_ws,
                    norm_tier=norm_tier,
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
                # Only newly added tickers (existing ones keep their manual tier)
                pending_reconcile_tickers = [
                    t for t in added_tickers if t not in live_by_ticker
                ]
                res_payload = {
                    "ok": True,
                    "group": group_row,
                    "resolved_count": len(resolved_valid_items),
                    "added_tickers": added_tickers,
                    "not_found": not_found,
                    "rejected": not_found,
                }
                if units_unavailable:
                    res_payload["units_unavailable"] = True

            elif act == "set_tier":
                assert norm_tier is not None
                updated_rows = await coverage_db.update_coverage_tier(
                    uid, norm_tickers, norm_tier, conn=conn
                )
                updated_tickers = [str(r["ticker"]) for r in updated_rows]
                updated_set = set(updated_tickers)
                missing_tickers = list(
                    dict.fromkeys(
                        [
                            *[t for t in snap_not_found if t not in updated_set],
                            *[t for t in norm_tickers if t not in updated_set],
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
                    "tier": norm_tier,
                }

            elif act == "remove":
                # Capture original workspace_id BEFORE deleting rows (Item 6)
                removed_ws_map: dict[str, str] = {}
                for sym in norm_tickers:
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
                    uid, norm_tickers, conn=conn
                )
                removed_set = set(removed)
                if removed_ws_map:
                    job_context["removed_workspaces"] = {
                        k: v for k, v in removed_ws_map.items() if k in removed_set
                    }
                missing_tickers = list(
                    dict.fromkeys(
                        [
                            *[t for t in snap_not_found if t not in removed_set],
                            *[t for t in norm_tickers if t not in removed_set],
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

            # Insert outbox record in the EXACT SAME transaction as the watchlist mutation (§5.3 Stage 1)
            job_row = await insert_reconcile_job(
                uid,
                trigger_action=act,
                target_policy_versions=target_versions,
                pending_tickers=pending_reconcile_tickers,
                override_user_pause=override_user_pause,
                job_context=job_context,
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
