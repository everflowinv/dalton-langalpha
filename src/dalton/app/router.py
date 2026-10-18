"""Dalton HTTP API router (`/api/v1/dalton/*`) for P0.7, P0.8, and PK.13.

Follows `src/server/AGENTS.md` layering: router orchestrates service calls only;
all SQL lives in `src/dalton/database/`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.dalton.budget.service import (
    get_budget_settings,
    get_budget_today,
    put_scoped_task_cap,
    remove_scoped_task_cap,
    save_budget_settings,
)
from src.dalton.services.openclaw_admin import (
    get_openclaw_status,
    probe_health,
    run_sync,
)
from src.dalton.settings import get_dalton_settings
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dalton", tags=["Dalton"])


class OpenClawSyncRequest(BaseModel):
    dry_run: bool = False
    force: bool = False


class BudgetSettingsUpdateRequest(BaseModel):
    daily_cap_usd: float = Field(..., ge=0.0)
    default_task_cap_usd: float | None = Field(default=None, gt=0.0)
    timezone: str = Field(default="Asia/Shanghai")


class TaskCapUpdateRequest(BaseModel):
    cap_usd: float = Field(..., gt=0.0)


class KnowledgeImportRequest(BaseModel):
    content: dict[str, Any]
    reason: str = "Legacy knowledge import"
    evidence_refs: list[str] = Field(default_factory=list)


class DaltonAutomationWriteRequest(BaseModel):
    workspace_id: str
    entry: dict[str, Any]


# ---------------------------------------------------------------------------
# P0.7: OpenClaw Broker v2 Model Sync, Status & Health
# ---------------------------------------------------------------------------


@router.post("/openclaw/models/sync")
@handle_api_exceptions("sync openclaw models", logger)
async def sync_openclaw_models_endpoint(
    body: OpenClawSyncRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await run_sync(user_id=user_id, dry_run=body.dry_run, force=body.force)


@router.get("/openclaw/models/status")
@handle_api_exceptions("get openclaw models status", logger)
async def get_openclaw_models_status_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await get_openclaw_status()


@router.get("/openclaw/health")
@handle_api_exceptions("probe openclaw health", logger)
async def get_openclaw_health_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    return await probe_health()


# ---------------------------------------------------------------------------
# P0.8: Local Daily USD Budget & Task Caps
# ---------------------------------------------------------------------------


@router.get("/budget")
@handle_api_exceptions("get dalton budget settings", logger)
async def get_budget_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    return await get_budget_settings(user_id)


@router.put("/budget")
@handle_api_exceptions("update dalton budget settings", logger)
async def update_budget_endpoint(
    body: BudgetSettingsUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    try:
        return await save_budget_settings(
            user_id,
            daily_cap_usd=body.daily_cap_usd,
            default_task_cap_usd=body.default_task_cap_usd,
            timezone_str=body.timezone,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/budget/task-caps/{scope_kind}/{scope_id}")
@handle_api_exceptions("upsert dalton task cap", logger)
async def upsert_task_cap_endpoint(
    scope_kind: str,
    scope_id: str,
    body: TaskCapUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    try:
        return await put_scoped_task_cap(user_id, scope_kind, scope_id, body.cap_usd)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/budget/task-caps/{scope_kind}/{scope_id}")
@handle_api_exceptions("delete dalton task cap", logger)
async def delete_task_cap_endpoint(
    scope_kind: str,
    scope_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    deleted = await remove_scoped_task_cap(user_id, scope_kind, scope_id)
    return {"deleted": deleted}


@router.get("/budget/today")
@handle_api_exceptions("get dalton budget today", logger)
async def get_budget_today_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    return await get_budget_today(user_id)


# ---------------------------------------------------------------------------
# PK.7 & PK.13: Knowledge Versions, Verdicts, Publications, Board & Autonomy
# ---------------------------------------------------------------------------


@router.get("/knowledge/{workspace_id}/{doc}/versions")
@handle_api_exceptions("list knowledge versions", logger)
async def list_knowledge_versions_endpoint(
    workspace_id: str,
    doc: str,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=200),
    before: int | None = Query(default=None),
) -> dict[str, Any]:
    from src.dalton.knowledge import service as knowledge_service

    items = await knowledge_service.list_doc_versions(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc,
        limit=limit,
        before=before,
    )
    return {"items": items}


@router.get("/knowledge/{workspace_id}/{doc}/versions/{n}")
@handle_api_exceptions("get knowledge version", logger)
async def get_knowledge_version_endpoint(
    workspace_id: str,
    doc: str,
    n: int,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.knowledge import service as knowledge_service

    row = await knowledge_service.get_doc_version(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc,
        version_no=n,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge version not found")
    return row


@router.get("/knowledge/{workspace_id}/{doc}/diff")
@handle_api_exceptions("diff knowledge versions", logger)
async def diff_knowledge_versions_endpoint(
    workspace_id: str,
    doc: str,
    user_id: CurrentUserId,
    from_version: int = Query(..., alias="from"),
    to_version: int = Query(..., alias="to"),
) -> dict[str, Any]:
    from src.dalton.knowledge import service as knowledge_service

    return await knowledge_service.diff_doc_versions(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc,
        from_version=from_version,
        to_version=to_version,
    )


@router.post("/knowledge/{workspace_id}/{doc}/import")
@handle_api_exceptions("import knowledge document", logger)
async def import_knowledge_document_endpoint(
    workspace_id: str,
    doc: str,
    body: KnowledgeImportRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    settings = get_dalton_settings()
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    if user_id not in settings.knowledge.pm_user_ids:
        raise HTTPException(status_code=403, detail="Only PM users may import knowledge")

    from src.dalton.knowledge import service as knowledge_service

    return await knowledge_service.import_document(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc,
        content=body.content,
        reason=body.reason,
        evidence_refs=body.evidence_refs,
    )


@router.get("/verdicts")
@handle_api_exceptions("list verdicts", logger)
async def list_verdicts_endpoint(
    user_id: CurrentUserId,
    workspace_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    from src.dalton.database import verdicts as verdicts_db

    if workspace_id:
        items = await verdicts_db.list_verdicts(
            workspace_id=workspace_id, limit=limit, since=since
        )
        items = [r for r in items if str(r.get("user_id")) == user_id]
    else:
        items = await verdicts_db.list_user_verdicts(
            user_id=user_id, since=since, until=until, limit=limit
        )
    return {"items": items}


@router.get("/publications")
@handle_api_exceptions("list publications", logger)
async def list_publications_endpoint(
    user_id: CurrentUserId,
    workspace_id: str | None = Query(default=None),
) -> dict[str, Any]:
    from src.dalton.database import publications as pub_db

    items = await pub_db.list_publications(
        workspace_id=workspace_id, user_id=user_id
    )
    return {"items": items}


@router.get("/publications/{publication_id}")
@handle_api_exceptions("get publication", logger)
async def get_publication_endpoint(
    publication_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.database import publications as pub_db

    row = await pub_db.get_publication(publication_id)
    if row is None or str(row.get("user_id")) != user_id:
        raise HTTPException(status_code=404, detail="Publication not found")
    return row


@router.get("/publish-requests")
@handle_api_exceptions("list publish requests", logger)
async def list_publish_requests_endpoint(
    user_id: CurrentUserId,
    status: str = Query(default="awaiting_pm"),
) -> dict[str, Any]:
    from src.dalton.database import publications as pub_db

    items = await pub_db.list_pending(user_id=user_id, status=status)
    return {"items": items}


@router.put("/automations/{name}")
@handle_api_exceptions("schedule dalton automation", logger)
async def put_dalton_automation_endpoint(
    name: str,
    body: DaltonAutomationWriteRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.tools.director_tools import schedule_dalton_automation_helper

    return await schedule_dalton_automation_helper(
        user_id=user_id,
        workspace_id=body.workspace_id,
        name=name,
        entry=body.entry,
    )


@router.get("/coverage/board")
@handle_api_exceptions("get coverage board", logger)
async def get_coverage_board_endpoint(
    user_id: CurrentUserId,
    tier: str = Query(default="all"),
    stale_only: bool = Query(default=False),
) -> dict[str, Any]:
    from src.dalton.autonomy.board import build_coverage_board

    return await build_coverage_board(
        user_id=user_id,
        tier=tier,
        stale_only=stale_only,
    )


@router.get("/autonomy/report")
@handle_api_exceptions("get autonomy weekly report", logger)
async def get_autonomy_report_endpoint(
    user_id: CurrentUserId,
    week: str | None = Query(default=None),
) -> dict[str, Any]:
    from src.dalton.autonomy.report import build_weekly_autonomy_report

    return await build_weekly_autonomy_report(user_id=user_id, week=week)
