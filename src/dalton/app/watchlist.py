"""FastAPI router for Personal Watchlist (`/api/v1/dalton/watchlist/*`, CORE_CAPABILITY)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.dalton.company_identity import reject_company_system_auth
from src.dalton.coverage import CoverageDataSourceError, list_user_coverage_summary
from src.dalton.settings import get_dalton_settings
from src.dalton.watchlist.policies import (
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
    label: str | None = None
    criteria: dict[str, Any] | None = None
    default_tier: str | None = None
    group_id: str | None = None
    tags: list[str] | None = None
    override_user_pause: bool = False


class WatchlistMutateRequest(BaseModel):
    action: str
    tickers: list[str] | str | None = None
    tier: str | None = None
    label: str | None = None
    criteria: dict[str, Any] | None = None
    default_tier: str | None = None
    auto_refresh: bool = True
    group_id: str | None = None
    tags: list[str] | None = None
    override_user_pause: bool = False
    expected_roster_hash: str | None = None
    expected_policy_versions: dict[str, int] | None = None
    expected_target_tickers: list[str] | None = None


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
    }


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
            label=body.label,
            criteria=body.criteria,
            default_tier=body.default_tier,
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

    expected_snapshot: dict[str, Any] | None = None
    if (
        body.expected_roster_hash is not None
        or body.expected_policy_versions is not None
    ):
        expected_snapshot = {
            "owner_user_id": uid,
            "expected_roster_hash": body.expected_roster_hash,
            "expected_policy_versions": body.expected_policy_versions,
            "target_tickers": body.expected_target_tickers,
        }

    try:
        return await WatchlistMutationService().execute_mutation(
            uid,
            action=body.action,
            tickers=body.tickers,
            tier=body.tier,
            source="ui",
            label=body.label,
            criteria=body.criteria,
            default_tier=body.default_tier,
            auto_refresh=body.auto_refresh,
            group_id=body.group_id,
            tags=body.tags,
            override_user_pause=body.override_user_pause,
            expected_snapshot=expected_snapshot,
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
