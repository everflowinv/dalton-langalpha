"""Dalton HTTP API router (`/api/v1/dalton/*`) for P0.7, P0.8, PK.13, P0N.8, and G2.

Follows `src/server/AGENTS.md` layering: router orchestrates service calls only;
all SQL lives in `src/dalton/database/`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.budget.service import (
    get_budget_settings,
    get_budget_today,
    put_scoped_task_cap,
    remove_scoped_task_cap,
    save_budget_settings,
)
from src.dalton.services.native_admin import (
    get_native_status,
    run_native_check,
    run_native_sync,
)
from src.dalton.services.openclaw_admin import (
    get_openclaw_status,
    probe_health,
    run_sync,
)
from src.dalton.services.provider_status import (
    get_available_models_report,
    get_providers_status,
    probe_provider,
)
from src.dalton.settings import get_dalton_settings
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dalton", tags=["Dalton"])


class OpenClawSyncRequest(BaseModel):
    dry_run: bool = False
    force: bool = False


class NativeModelSyncRequest(BaseModel):
    models: list[str] = Field(min_length=1)


class ProviderProbeRequest(BaseModel):
    model_key: str | None = None


class BudgetSettingsUpdateRequest(BaseModel):
    daily_cap_usd: float = Field(..., ge=0.0)
    default_task_cap_usd: float | None = Field(default=None, gt=0.0)
    timezone: str = Field(default="Asia/Shanghai")


class TaskCapUpdateRequest(BaseModel):
    cap_usd: float = Field(..., gt=0.0)


class KnowledgeImportRequest(BaseModel):
    content: dict[str, Any]
    reason: str = "Legacy knowledge import"
    evidence_refs: list[Any] = Field(default_factory=list)


class DaltonAutomationWriteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspace_id: str | None = None
    entry: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


def _require_pm_user(user_id: str) -> None:
    _require_dalton_enabled()
    settings = get_dalton_settings()
    if user_id not in settings.pm_user_ids:
        raise HTTPException(
            status_code=403, detail="Only PM users may perform this action"
        )


async def _verify_workspace_access(workspace_id: str, user_id: str) -> dict[str, Any]:
    """Verify that ``workspace_id`` belongs to ``user_id`` fail-closed (G2 #2, R4-8, R5-1, P0J §8.1 #14)."""
    _require_dalton_enabled()
    from src.dalton.auth import verify_workspace_access

    return await verify_workspace_access(workspace_id, user_id)


# ---------------------------------------------------------------------------
# P0.7: OpenClaw Broker v2 Model Sync, Status & Health
# ---------------------------------------------------------------------------


@router.post("/openclaw/models/sync")
@handle_api_exceptions("sync openclaw models", logger)
async def sync_openclaw_models_endpoint(
    body: OpenClawSyncRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    return await run_sync(user_id=user_id, dry_run=body.dry_run, force=body.force)


@router.get("/openclaw/models/status")
@handle_api_exceptions("get openclaw models status", logger)
async def get_openclaw_models_status_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_openclaw_status()


@router.get("/openclaw/health")
@handle_api_exceptions("probe openclaw health", logger)
async def get_openclaw_health_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await probe_health(user_id=user_id)


# ---------------------------------------------------------------------------
# Native-subscription model sync (check → confirm → add-only runtime overlay)
# ---------------------------------------------------------------------------


@router.get("/native-models/status")
@handle_api_exceptions("get native models status", logger)
async def get_native_models_status_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_native_status()


@router.post("/native-models/check")
@handle_api_exceptions("check native provider models", logger)
async def check_native_models_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    return await run_native_check(user_id=user_id)


@router.post("/native-models/sync")
@handle_api_exceptions("sync native provider models", logger)
async def sync_native_models_endpoint(
    body: NativeModelSyncRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    return await run_native_sync(user_id=user_id, model_keys=body.models)


# ---------------------------------------------------------------------------
# P0N.8: Native Model Provider Status, Probe & Available Models
# ---------------------------------------------------------------------------


@router.get("/providers/status")
@handle_api_exceptions("get dalton providers status", logger)
async def get_providers_status_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_providers_status(user_id=user_id)


@router.post("/providers/{provider}/probe")
@handle_api_exceptions("probe dalton provider", logger)
async def probe_provider_endpoint(
    provider: str,
    user_id: CurrentUserId,
    body: ProviderProbeRequest | None = None,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await probe_provider(
        user_id=user_id,
        provider=provider,
        model_key=body.model_key if body else None,
    )


@router.get("/providers/available-models")
@handle_api_exceptions("get dalton available models", logger)
async def get_available_models_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_available_models_report(user_id=user_id)


# ---------------------------------------------------------------------------
# P0.8: Local Daily USD Budget & Task Caps
# ---------------------------------------------------------------------------


@router.get("/budget")
@handle_api_exceptions("get dalton budget settings", logger)
async def get_budget_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_budget_settings(user_id)


@router.put("/budget")
@handle_api_exceptions("update dalton budget settings", logger)
async def update_budget_endpoint(
    body: BudgetSettingsUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
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
    _require_dalton_enabled()
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
    _require_dalton_enabled()
    deleted = await remove_scoped_task_cap(user_id, scope_kind, scope_id)
    return {"deleted": deleted}


@router.get("/budget/today")
@handle_api_exceptions("get dalton budget today", logger)
async def get_budget_today_endpoint(user_id: CurrentUserId) -> dict[str, Any]:
    _require_dalton_enabled()
    return await get_budget_today(user_id=user_id)


# ---------------------------------------------------------------------------
# PK.7 & PK.13: Knowledge Versions, Verdicts, Publications, Board & Autonomy
# ---------------------------------------------------------------------------


@router.get("/workspaces/team")
@handle_api_exceptions("list team workspaces", logger)
async def list_team_workspaces_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.sharing import list_team_workspaces

    members = await list_team_workspaces(user_id)
    return {"members": members}


@router.get("/credentials/status")
@handle_api_exceptions("get team credentials status", logger)
async def get_team_credentials_status_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """Return team credential sharing status and caller disconnect permission (`DALTON-HOOK(team-credentials)`)."""
    from src.dalton.credentials import (
        credential_user_id,
        get_team_credential_holder_id,
        is_credential_admin,
        is_team_credential_sharing_enabled,
    )
    from src.dalton.settings import get_dalton_config

    cfg = get_dalton_config()
    enabled = is_team_credential_sharing_enabled()
    can_disconnect = is_credential_admin(user_id)
    return {
        "sharing": cfg.credentials.sharing,
        "enabled": enabled,
        "holder_user_id": get_team_credential_holder_id(),
        "credential_user_id": credential_user_id(user_id),
        "is_pm": can_disconnect,
        "can_disconnect": can_disconnect,
    }


@router.get("/knowledge/{workspace_id}/{doc}/versions")
@handle_api_exceptions("list knowledge versions", logger)
async def list_knowledge_versions_endpoint(
    workspace_id: str,
    doc: str,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=200),
    before: int | None = Query(default=None),
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_reader

    if is_dalton_supabase_auth_enabled():
        workspace = await require_workspace_reader(workspace_id, user_id)
    else:
        workspace = await _verify_workspace_access(workspace_id, user_id)
    owner_id = str((workspace or {}).get("user_id") or user_id)
    rows = await db_knowledge.list_versions(
        workspace_id=workspace_id,
        doc_key=doc,
        limit=limit,
        before=before,
    )
    items = [
        r
        for r in rows
        if not r.get("user_id") or str(r.get("user_id")) in {str(user_id), owner_id}
    ]
    return {"items": items}


@router.get("/knowledge/{workspace_id}/{doc}/versions/{n}")
@handle_api_exceptions("get knowledge version", logger)
async def get_knowledge_version_endpoint(
    workspace_id: str,
    doc: str,
    n: int,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_reader

    if is_dalton_supabase_auth_enabled():
        workspace = await require_workspace_reader(workspace_id, user_id)
    else:
        workspace = await _verify_workspace_access(workspace_id, user_id)
    owner_id = str((workspace or {}).get("user_id") or user_id)
    row = await db_knowledge.get_version(
        workspace_id=workspace_id,
        doc_key=doc,
        version_no=n,
    )
    if row is None or (
        row.get("user_id") and str(row.get("user_id")) not in {str(user_id), owner_id}
    ):
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
    _require_dalton_enabled()
    from src.dalton.knowledge import service as knowledge_service
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_reader

    if is_dalton_supabase_auth_enabled():
        await require_workspace_reader(workspace_id, user_id)
    else:
        await _verify_workspace_access(workspace_id, user_id)
    diffs = await knowledge_service.diff_versions(
        workspace_id=workspace_id,
        doc_key=doc,
        from_version=from_version,
        to_version=to_version,
    )
    return {
        "workspace_id": workspace_id,
        "doc": doc,
        "from_version": from_version,
        "to_version": to_version,
        "diffs": diffs,
    }


@router.post("/knowledge/{workspace_id}/{doc}/import")
@handle_api_exceptions("import knowledge document", logger)
async def import_knowledge_document_endpoint(
    workspace_id: str,
    doc: str,
    body: KnowledgeImportRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    await _verify_workspace_access(workspace_id, user_id)
    from src.dalton.knowledge import service as knowledge_service

    provenance = [
        r if isinstance(r, dict) else {"kind": "note", "ref": str(r)}
        for r in (body.evidence_refs or [])
    ]
    return await knowledge_service.import_knowledge_doc(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc,
        content=body.content,
        reason=body.reason,
        provenance=provenance,
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
    _require_dalton_enabled()
    from src.dalton.database import verdicts as verdicts_db
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_reader

    target_user_id = user_id
    if workspace_id:
        if is_dalton_supabase_auth_enabled():
            workspace = await require_workspace_reader(workspace_id, user_id)
        else:
            workspace = await _verify_workspace_access(workspace_id, user_id)
        target_user_id = str((workspace or {}).get("user_id") or user_id)
    items = await verdicts_db.list_user_verdicts(
        user_id=target_user_id,
        workspace_id=workspace_id,
        since=since,
        until=until,
        limit=limit,
    )
    return {"items": items}


@router.get("/publications")
@handle_api_exceptions("list publications", logger)
async def list_publications_endpoint(
    user_id: CurrentUserId,
    workspace_id: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.database import publications as pub_db
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_reader

    target_user_id: str | None = user_id
    if workspace_id:
        if is_dalton_supabase_auth_enabled():
            workspace = await require_workspace_reader(workspace_id, user_id)
        else:
            workspace = await _verify_workspace_access(workspace_id, user_id)
        target_user_id = str((workspace or {}).get("user_id") or user_id)
    items = await pub_db.list_publications(
        workspace_id=workspace_id, user_id=target_user_id
    )
    return {"items": items}


@router.get("/publications/{publication_id}")
@handle_api_exceptions("get publication", logger)
async def get_publication_endpoint(
    publication_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.database import publications as pub_db
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_team_reader, require_workspace_reader

    row = await pub_db.get_publication(publication_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    if is_dalton_supabase_auth_enabled():
        if row.get("workspace_id"):
            await require_workspace_reader(str(row["workspace_id"]), user_id)
            pub_owner = str(row.get("user_id") or "").strip()
            if pub_owner and pub_owner != str(user_id):
                await require_team_reader(pub_owner)
        else:
            await require_team_reader(user_id)
            if str(row.get("user_id")) != str(user_id):
                raise HTTPException(status_code=403, detail="Forbidden")
    elif str(row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=404, detail="Publication not found")
    return row


@router.get("/publish-requests")
@handle_api_exceptions("list publish requests", logger)
async def list_publish_requests_endpoint(
    user_id: CurrentUserId,
    status: str = Query(default="awaiting_pm"),
) -> dict[str, Any]:
    _require_dalton_enabled()
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
    _require_dalton_enabled()
    from src.dalton.auth import resolve_caller_home_workspace_id
    from src.dalton.tools.director_tools import schedule_dalton_automation_helper

    spec = body.entry or body.spec or body.model_dump(exclude={"workspace_id", "entry", "spec"}, exclude_none=True)
    raw_ws = body.workspace_id if body.workspace_id is not None else (spec or {}).get("workspace_id")
    ws_str = str(raw_ws or "").strip()
    if not ws_str or ws_str.lower() == "home":
        resolved_ws_id = await resolve_caller_home_workspace_id(user_id)
    else:
        await _verify_workspace_access(ws_str, user_id)
        resolved_ws_id = ws_str
    return await schedule_dalton_automation_helper(
        user_id=user_id,
        name=name,
        spec=spec,
        workspace_id=resolved_ws_id,
    )


@router.get("/coverage/board")
@handle_api_exceptions("get coverage board", logger)
async def get_coverage_board_endpoint(
    user_id: CurrentUserId,
    tier: str = Query(default="all"),
    stale_only: bool = Query(default=False),
) -> dict[str, Any]:
    _require_dalton_enabled()
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
    _require_dalton_enabled()
    from src.dalton.autonomy.report import generate_weekly_autonomy_report

    return await generate_weekly_autonomy_report(user_id=user_id, week=week)
