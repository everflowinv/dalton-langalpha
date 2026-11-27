"""FastAPI router for Personal Watchlist (`/api/v1/dalton/watchlist/*`, CORE_CAPABILITY)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.dalton.company_identity import reject_company_system_auth
from src.dalton.coverage import CoverageDataSourceError, list_user_coverage_summary
from src.dalton.settings import get_dalton_settings
from src.dalton.watchlist.metrics import WatchlistMetricsService
from src.dalton.watchlist.policies import (
    DEFAULT_TIER_POLICIES,
    get_tier_policy_presets,
    get_user_tier_policies,
    list_unfinished_reconcile_jobs,
)
from src.dalton.watchlist.reconciler import TierPolicyReconciler
from src.dalton.watchlist.service import (
    WatchlistMutationService,
    WatchlistScopeChangedError,
)
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


router = APIRouter(
    prefix="/api/v1/dalton/watchlist",
    tags=["Dalton Watchlist"],
    dependencies=[Depends(_require_dalton_enabled)],
)


class WatchlistPreviewRequest(BaseModel):
    action: str
    tickers: list[str] | str | None = None
    tier: str | None = None
    policy: dict[str, Any] | None = None
    label: str | None = None
    criteria: dict[str, Any] | None = None
    default_tier: str | None = None
    auto_refresh: bool = True
    group_id: str | None = None
    tags: list[str] | None = None
    override_user_pause: bool = False


class WatchlistMutateRequest(BaseModel):
    action: str
    snapshot_id: str | None = None
    tickers: list[str] | str | None = None
    tier: str | None = None
    policy: dict[str, Any] | None = None
    expected_policy_version: int | None = None
    expected_roster_hash: str | None = None
    label: str | None = None
    criteria: dict[str, Any] | None = None
    default_tier: str | None = None
    auto_refresh: bool | None = None
    group_id: str | None = None
    tags: list[str] | None = None
    override_user_pause: bool | None = None


class TierPolicyPreviewRequest(BaseModel):
    tier: str
    action: str = "set_tier_policy"
    policy: dict[str, Any] | None = None
    watch_enabled: bool | None = None
    watch_cron: str | None = None
    delta_enabled: bool | None = None
    delta_cron: str | None = None
    delta_timezone: str | None = None
    move_enabled: bool | None = None
    move_threshold_pct: float | None = None
    earnings_stages: list[str] | None = None
    override_user_pause: bool = False


class TierPolicyCommitRequest(BaseModel):
    snapshot_id: str
    expected_policy_version: int | None = None
    expected_roster_hash: str | None = None
    override_user_pause: bool | None = None


def _extract_tier_policy_patch(body: TierPolicyPreviewRequest) -> dict[str, Any]:
    patch: dict[str, Any] = dict(body.policy or {})
    for field in (
        "watch_enabled",
        "watch_cron",
        "delta_enabled",
        "delta_cron",
        "delta_timezone",
        "move_enabled",
        "move_threshold_pct",
        "earnings_stages",
    ):
        val = getattr(body, field, None)
        if val is not None:
            patch[field] = val
    return patch


class WatchlistReconcileRequest(BaseModel):
    job_id: str | None = Field(default=None, description="Optional single outbox job UUID to retry")
    sync_home: bool = True


@router.get("")
@handle_api_exceptions("get personal watchlist", logger)
async def get_watchlist_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    summary = await list_user_coverage_summary(uid)
    policies = await get_user_tier_policies(uid)
    try:
        unfinished_jobs = await list_unfinished_reconcile_jobs(uid)
    except Exception:
        unfinished_jobs = []
    return {
        **summary,
        "tier_policies": {t: p.to_dict() for t, p in policies.items()},
        "pending_reconcile_count": len(unfinished_jobs),
    }


@router.get("/metrics")
@handle_api_exceptions("get personal watchlist metrics", logger)
async def get_watchlist_metrics_endpoint(
    user_id: CurrentUserId,
    symbols: str | None = Query(default=None, description="Optional comma-separated ticker filter"),
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    sym_list = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else None
    )
    return await WatchlistMetricsService().get_metrics_for_user(uid, symbols=sym_list)


@router.get("/tier-policies")
@handle_api_exceptions("get personal watchlist tier policies", logger)
async def get_watchlist_tier_policies_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    policies = await get_user_tier_policies(uid)
    return {
        "ok": True,
        "user_id": uid,
        "policies": {t: p.to_dict() for t, p in policies.items()},
        "defaults": {t: p.to_dict() for t, p in DEFAULT_TIER_POLICIES.items()},
        "presets": get_tier_policy_presets(),
    }


@router.post("/tier-policies/preview")
@handle_api_exceptions("preview personal watchlist tier policy change", logger)
async def preview_watchlist_tier_policy_endpoint(
    body: TierPolicyPreviewRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    act = str(body.action or "set_tier_policy").strip().lower()
    if act not in ("set_tier_policy", "reset_tier_policy"):
        raise HTTPException(
            status_code=400,
            detail="action 仅支持 set_tier_policy 或 reset_tier_policy。",
        )
    patch = _extract_tier_policy_patch(body) if act == "set_tier_policy" else None
    try:
        return await WatchlistMutationService().preview_mutation(
            uid,
            action=act,
            tier=body.tier,
            policy=patch,
            override_user_pause=body.override_user_pause,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/tier-policies/{tier}")
@handle_api_exceptions("update personal watchlist tier policy", logger)
async def update_watchlist_tier_policy_endpoint(
    tier: str,
    body: TierPolicyCommitRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    try:
        return await WatchlistMutationService().execute_mutation(
            uid,
            action="set_tier_policy",
            snapshot_id=body.snapshot_id,
            tier=tier,
            expected_policy_version=body.expected_policy_version,
            expected_roster_hash=body.expected_roster_hash,
            override_user_pause=body.override_user_pause,
            source="ui",
        )
    except WatchlistScopeChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tier-policies/{tier}/reset")
@handle_api_exceptions("reset personal watchlist tier policy", logger)
async def reset_watchlist_tier_policy_endpoint(
    tier: str,
    body: TierPolicyCommitRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    try:
        return await WatchlistMutationService().execute_mutation(
            uid,
            action="reset_tier_policy",
            snapshot_id=body.snapshot_id,
            tier=tier,
            expected_policy_version=body.expected_policy_version,
            expected_roster_hash=body.expected_roster_hash,
            override_user_pause=body.override_user_pause,
            source="ui",
        )
    except WatchlistScopeChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/preview")
@handle_api_exceptions("preview personal watchlist mutation", logger)
async def preview_watchlist_mutation_endpoint(
    body: WatchlistPreviewRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    try:
        preview = await WatchlistMutationService().preview_mutation(
            uid,
            action=body.action,
            tickers=body.tickers,
            tier=body.tier,
            policy=body.policy,
            label=body.label,
            criteria=body.criteria,
            default_tier=body.default_tier,
            auto_refresh=body.auto_refresh,
            group_id=body.group_id,
            tags=body.tags,
            override_user_pause=body.override_user_pause,
        )
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preview


@router.post("/mutate")
@handle_api_exceptions("mutate personal watchlist", logger)
async def mutate_watchlist_endpoint(
    body: WatchlistMutateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    if str(body.action or "").strip().lower() == "resolve_group":
        raise HTTPException(
            status_code=400,
            detail="resolve_group is an internal-only action and cannot be invoked via /watchlist/mutate",
        )

    try:
        return await WatchlistMutationService().execute_mutation(
            uid,
            action=body.action,
            snapshot_id=body.snapshot_id,
            tickers=body.tickers,
            tier=body.tier,
            policy=body.policy,
            expected_policy_version=body.expected_policy_version,
            expected_roster_hash=body.expected_roster_hash,
            source="ui",
            label=body.label,
            criteria=body.criteria,
            default_tier=body.default_tier,
            auto_refresh=body.auto_refresh,
            group_id=body.group_id,
            tags=body.tags,
            override_user_pause=body.override_user_pause,
        )
    except WatchlistScopeChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reconcile")
@handle_api_exceptions("reconcile personal watchlist outbox jobs", logger)
async def reconcile_watchlist_endpoint(
    user_id: CurrentUserId,
    body: WatchlistReconcileRequest | None = None,
) -> dict[str, Any]:
    uid = str(user_id).strip()
    reject_company_system_auth(uid)
    req = body or WatchlistReconcileRequest()
    reconciler = TierPolicyReconciler()
    if req.job_id:
        res = await reconciler.reconcile_job(uid, req.job_id, sync_home=req.sync_home)
        if res.get("error") == "job_not_found":
            raise HTTPException(status_code=404, detail="Reconcile job not found")
        return res
    return await reconciler.reconcile_pending_jobs(uid, sync_home=req.sync_home)
