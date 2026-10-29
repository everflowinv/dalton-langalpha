"""Dalton HTTP API router (`/api/v1/dalton/*`) for P0.7, P0.8, PK.13, P0N.8, and G2.

Follows `src/server/AGENTS.md` layering: router orchestrates service calls only;
all SQL lives in `src/dalton/database/`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    run_native_retire,
    run_native_restore,
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


class NativeModelKeysRequest(BaseModel):
    keys: list[str] = Field(min_length=1)


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


class WorkspaceTrackingUpdateRequest(BaseModel):
    enabled: bool
    tier: str | None = None


class WorkspaceSharingUpdateRequest(BaseModel):
    cos_access: str | None = None
    web_access: str | None = None


class UserSharingUpdateRequest(BaseModel):
    cos_rw_enabled: bool


class SystemJobToggleRequest(BaseModel):
    enabled: bool


class CoverageMutateRequest(BaseModel):
    action: str
    tickers: list[str] | str
    tier: str = "watch"


class CoverageGroupPreviewRequest(BaseModel):
    label: str = "未命名规则组"
    criteria: dict[str, Any] = Field(default_factory=dict)


class CoverageGroupCreateRequest(BaseModel):
    label: str
    criteria: dict[str, Any] = Field(default_factory=dict)
    default_tier: str = "watch"
    auto_refresh: bool = True


class EarningsCalendarMirrorRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    ledger: dict[str, Any] | None = None
    updated: str | None = None
    entries: dict[str, Any] | None = None
    reported: list[Any] | None = None


def _require_dalton_enabled() -> None:
    if not get_dalton_settings().enabled:
        raise HTTPException(status_code=404, detail="Dalton mode disabled")


def _require_pm_user(user_id: str) -> None:
    _require_dalton_enabled()
    from src.dalton.autonomy.system_jobs import require_pm_user

    try:
        require_pm_user(user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


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


@router.get("/diagnostics/browser-memory")
@handle_api_exceptions("get browser memory diagnostics", logger)
async def get_browser_memory_diagnostics_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    from src.observability.browser_memory import get_browser_memory_diagnostics

    return get_browser_memory_diagnostics()


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


@router.post("/native-models/retire")
@handle_api_exceptions("retire native provider models", logger)
async def retire_native_models_endpoint(
    body: NativeModelKeysRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    return await run_native_retire(user_id=user_id, model_keys=body.keys)


@router.post("/native-models/restore")
@handle_api_exceptions("restore retired native models", logger)
async def restore_native_models_endpoint(
    body: NativeModelKeysRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_pm_user(user_id)
    return await run_native_restore(user_id=user_id, model_keys=body.keys)


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
    from src.dalton.access_tiers import get_user_tier
    from src.dalton.settings import is_colleague_dispatch_enabled
    from src.dalton.sharing import list_team_workspaces

    members = await list_team_workspaces(user_id)
    # The sidebar hides the whole "团队" section for external members.
    caller_tier = await get_user_tier(user_id)
    return {
        "members": members,
        "caller_tier": caller_tier,
        "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
    }


@router.get("/workspaces/{workspace_id}/sharing")
@handle_api_exceptions("get workspace sharing settings", logger)
async def get_workspace_sharing_endpoint(
    workspace_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.server.database.home_workspace import is_home_row
    from src.dalton.access_tiers import classify_owner_tier, get_user_tier
    from src.dalton.settings import (
        is_colleague_dispatch_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import (
        can_edit_workspace_sharing,
        get_workspace_sharing,
        require_team_reader,
        require_workspace_access,
    )
    from src.server.database import workspace as ws_db

    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    is_pm = uid in get_dalton_settings().pm_user_ids
    if is_dalton_supabase_auth_enabled() and not is_pm:
        await require_team_reader(uid)

    ws = await ws_db.get_workspace(workspace_id)
    if ws is None or ws.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="Workspace not found")

    is_home = is_home_row(ws) or bool(ws.get("is_home"))
    owner_id = str(ws.get("user_id") or "").strip()

    # Classify the owner exactly once per request, before the Home branch and
    # any can_edit decision, so isolation and the owner_external flag can
    # never disagree: external callers and non-admins on external-owned (or
    # unresolvable) workspaces get 404; an external-owned Home is 404 even
    # for admins (never shown to anyone but its owner).
    owner_tier: str | None = None
    if is_dalton_supabase_auth_enabled() and owner_id and owner_id != uid:
        caller_tier = await get_user_tier(uid)
        if caller_tier == "external":
            raise HTTPException(status_code=404, detail="Workspace not found")
        owner_tier = await classify_owner_tier(owner_id)
        if owner_tier == "unknown":
            raise HTTPException(status_code=404, detail="Workspace not found")
        if owner_tier == "external" and is_home:
            raise HTTPException(status_code=404, detail="Workspace not found")

    if is_home:
        if owner_id != uid:
            raise HTTPException(
                status_code=403,
                detail="Home workspace is not shared with team members",
            )
        return {
            "workspace_id": str(ws["workspace_id"]),
            "cos_access": "hidden",
            "web_access": "hidden",
            "is_home": True,
            "can_edit": False,
            "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
            "updated_by": None,
            "updated_at": None,
        }

    effective_owner_tier = owner_tier
    if (
        effective_owner_tier is None
        and is_dalton_supabase_auth_enabled()
        and owner_id
    ):
        effective_owner_tier = await classify_owner_tier(owner_id)
    owner_external = effective_owner_tier == "external"

    if owner_external:
        if owner_id != uid:
            if ws.get("status") in ("unbound", "deleted"):
                raise HTTPException(status_code=404, detail="Workspace not found")
            await require_team_reader(owner_id)
            sharing = await get_workspace_sharing(str(ws["workspace_id"]))
            if sharing.get("web_access") == "hidden":
                raise HTTPException(status_code=404, detail="Workspace not found")
            return {
                **sharing,
                "cos_access": "hidden",
                "is_home": False,
                "can_edit": False,
                "owner_external": True,
                "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
            }
        sharing = await get_workspace_sharing(str(ws["workspace_id"]))
        return {
            **sharing,
            "cos_access": "hidden",
            "is_home": False,
            "can_edit": True,
            "owner_external": True,
            "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
        }

    can_edit = can_edit_workspace_sharing(ws, uid)
    if not can_edit:
        if is_dalton_supabase_auth_enabled():
            await require_workspace_access(
                ws,
                uid,
                channel="web",
                need="read",
                workspace=ws,
            )
        else:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to access this workspace",
            )

    sharing = await get_workspace_sharing(str(ws["workspace_id"]))
    return {
        **sharing,
        "is_home": False,
        "can_edit": can_edit,
        "owner_external": False,
        "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
    }


@router.put("/workspaces/{workspace_id}/sharing")
@handle_api_exceptions("update workspace sharing settings", logger)
async def update_workspace_sharing_endpoint(
    workspace_id: str,
    body: WorkspaceSharingUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.server.database.home_workspace import is_home_row
    from src.dalton.settings import (
        is_colleague_dispatch_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import (
        VALID_ACCESS_LEVELS,
        can_edit_workspace_sharing,
        get_workspace_sharing,
        require_team_reader,
        upsert_workspace_sharing,
    )
    from src.server.database import workspace as ws_db

    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    is_pm = uid in get_dalton_settings().pm_user_ids
    if is_dalton_supabase_auth_enabled() and not is_pm:
        await require_team_reader(uid)

    ws = await ws_db.get_workspace(workspace_id)
    if ws is None or ws.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="Workspace not found")

    is_home = is_home_row(ws) or bool(ws.get("is_home"))
    owner_id = str(ws.get("user_id") or "").strip()

    # Classify the owner exactly once per request and reuse the result below:
    # a non-owner external caller always gets 404 (Home included); internal
    # callers get 404 for external-owned and unresolvable workspaces; an
    # external-owned Home is 404 even for admins (never shown to anyone).
    owner_tier: str | None = None
    if is_dalton_supabase_auth_enabled() and owner_id and owner_id != uid:
        from src.dalton.access_tiers import classify_owner_tier, get_user_tier

        caller_tier = await get_user_tier(uid)
        if caller_tier == "external":
            raise HTTPException(status_code=404, detail="Workspace not found")
        owner_tier = await classify_owner_tier(owner_id)
        if owner_tier == "unknown":
            raise HTTPException(status_code=404, detail="Workspace not found")
        if owner_tier == "external" and is_home:
            raise HTTPException(status_code=404, detail="Workspace not found")

    if is_home:
        if owner_id != uid and not is_pm:
            raise HTTPException(
                status_code=403,
                detail="Home workspace is not shared with team members",
            )
        raise HTTPException(
            status_code=400,
            detail="Home workspace cannot be shared",
        )

    effective_owner_tier = owner_tier
    if (
        effective_owner_tier is None
        and is_dalton_supabase_auth_enabled()
        and owner_id
    ):
        from src.dalton.access_tiers import classify_owner_tier

        effective_owner_tier = await classify_owner_tier(owner_id)
    owner_external = effective_owner_tier == "external"
    if owner_external:
        if owner_id != uid:
            if ws.get("status") in ("unbound", "deleted"):
                raise HTTPException(status_code=404, detail="Workspace not found")
            await require_team_reader(owner_id)
            sharing = await get_workspace_sharing(str(ws["workspace_id"]))
            if sharing.get("web_access") == "hidden":
                raise HTTPException(status_code=404, detail="Workspace not found")
            raise HTTPException(
                status_code=403,
                detail="Only workspace owner may modify sharing settings",
            )
        if body.cos_access is not None:
            raise HTTPException(
                status_code=403,
                detail="外部成员不能开启或修改研究总监访问权限",
            )

    if not can_edit_workspace_sharing(ws, uid):
        raise HTTPException(
            status_code=403,
            detail="Only workspace owner or PM may modify sharing settings",
        )

    if body.cos_access is None and body.web_access is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of cos_access or web_access must be provided",
        )

    cos_val: str | None = None
    if body.cos_access is not None:
        cos_val = str(body.cos_access).strip().lower()
        if cos_val not in VALID_ACCESS_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cos_access: {body.cos_access!r}",
            )

    web_val: str | None = None
    if body.web_access is not None:
        web_val = str(body.web_access).strip().lower()
        if web_val not in VALID_ACCESS_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid web_access: {body.web_access!r}",
            )

    saved = await upsert_workspace_sharing(
        str(ws["workspace_id"]),
        cos_access="hidden" if owner_external else cos_val,
        web_access=web_val,
        updated_by=uid,
    )
    return {
        **saved,
        **({"cos_access": "hidden"} if owner_external else {}),
        "is_home": False,
        "can_edit": True,
        "owner_external": owner_external,
        "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
    }


@router.get("/sharing/user-settings")
@handle_api_exceptions("get user cos sharing settings", logger)
async def get_user_sharing_settings_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.access_tiers import get_user_tier
    from src.dalton.settings import (
        is_colleague_dispatch_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import get_user_cos_sharing, require_team_reader

    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    if is_dalton_supabase_auth_enabled():
        await require_team_reader(uid)
        caller_tier = await get_user_tier(uid)
        if caller_tier == "external":
            return {
                "user_id": uid,
                "cos_rw_enabled": False,
                "cos_access": "hidden",
                "can_edit": False,
                "caller_tier": caller_tier,
                "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
                "updated_at": None,
            }
    else:
        caller_tier = "internal"

    settings_row = await get_user_cos_sharing(uid)
    return {
        **settings_row,
        "can_edit": True,
        "caller_tier": caller_tier,
        "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
    }


@router.put("/sharing/user-settings")
@handle_api_exceptions("update user cos sharing settings", logger)
async def update_user_sharing_settings_endpoint(
    body: UserSharingUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.access_tiers import get_user_tier
    from src.dalton.settings import (
        is_colleague_dispatch_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import require_team_reader, upsert_user_cos_sharing

    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    if is_dalton_supabase_auth_enabled():
        await require_team_reader(uid)
        caller_tier = await get_user_tier(uid)
        if caller_tier == "external":
            raise HTTPException(
                status_code=403,
                detail="外部成员的工作区仅管理员可查看，不提供共享设置",
            )
    else:
        caller_tier = "internal"

    saved = await upsert_user_cos_sharing(
        uid, cos_rw_enabled=body.cos_rw_enabled
    )
    return {
        **saved,
        "can_edit": True,
        "caller_tier": caller_tier,
        "colleague_dispatch_enabled": is_colleague_dispatch_enabled(),
    }


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
    from src.dalton.invites import get_user_tier
    from src.dalton.settings import get_dalton_config

    cfg = get_dalton_config()
    enabled = is_team_credential_sharing_enabled()
    can_disconnect = is_credential_admin(user_id)
    caller_tier = await get_user_tier(user_id)
    return {
        "sharing": cfg.credentials.sharing,
        "enabled": enabled,
        "holder_user_id": get_team_credential_holder_id(),
        "credential_user_id": credential_user_id(user_id),
        "is_pm": can_disconnect,
        "can_disconnect": can_disconnect,
        "can_invite": caller_tier in ("admin", "internal"),
        "caller_tier": caller_tier,
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
        await require_workspace_reader(workspace_id, user_id)
    else:
        await _verify_workspace_access(workspace_id, user_id)
    rows = await db_knowledge.list_versions(
        workspace_id=workspace_id,
        doc_key=doc,
        limit=limit,
        before=before,
    )
    return {"items": list(rows)}


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
        await require_workspace_reader(workspace_id, user_id)
    else:
        await _verify_workspace_access(workspace_id, user_id)
    row = await db_knowledge.get_version(
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
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.tools.director_tools import schedule_dalton_automation_helper

    spec = body.entry or body.spec or body.model_dump(exclude={"workspace_id", "entry", "spec"}, exclude_none=True)
    raw_ws = body.workspace_id if body.workspace_id is not None else (spec or {}).get("workspace_id")
    ws_str = str(raw_ws or "").strip()
    if not ws_str or ws_str.lower() == "home":
        resolved_ws_id = await resolve_caller_home_workspace_id(user_id)
    else:
        await _verify_workspace_access(ws_str, user_id)
        resolved_ws_id = ws_str
    try:
        return await schedule_dalton_automation_helper(
            user_id=user_id,
            name=name,
            spec=spec,
            workspace_id=resolved_ws_id,
        )
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


async def _resolve_tracking_workspace_owner(
    workspace_id: str,
    user_id: str,
    *,
    require_write: bool,
) -> tuple[str, bool]:
    """Verify workspace access for tracking and return ``(owner_user_id, can_edit)``."""
    _require_dalton_enabled()
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import (
        EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL,
        require_workspace_reader,
    )
    from src.server.database import workspace as ws_db

    uid = str(user_id or "").strip()
    is_pm = bool(uid and uid in get_dalton_settings().pm_user_ids)

    if is_dalton_supabase_auth_enabled():
        from src.dalton.access_tiers import classify_owner_tier

        workspace = await require_workspace_reader(workspace_id, uid)
        owner_id = str((workspace or {}).get("user_id") or uid).strip()
        if owner_id != uid:
            owner_tier = await classify_owner_tier(owner_id)
            if owner_tier == "unknown":
                raise HTTPException(
                    status_code=404,
                    detail="Workspace not found",
                )
            if owner_tier == "external":
                # External-owned workspaces are read-only even for admins, so
                # tracking stays under the owner's control.
                if require_write:
                    raise HTTPException(
                        status_code=403,
                        detail=EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL,
                    )
                return owner_id, False
        can_edit = (owner_id == uid) or is_pm
        if require_write and not can_edit:
            raise HTTPException(
                status_code=403,
                detail="Only workspace owner or PM may modify tracking",
            )
        return owner_id, can_edit

    try:
        workspace = await _verify_workspace_access(workspace_id, uid)
        owner_id = str((workspace or {}).get("user_id") or uid).strip()
        return owner_id, True
    except HTTPException as exc:
        if exc.status_code == 403 and is_pm:
            ws = await ws_db.get_workspace(workspace_id)
            if ws is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Workspace '{workspace_id}' not found",
                ) from exc
            owner_id = str(ws.get("user_id") or uid).strip()
            return owner_id, True
        raise


@router.get("/tracking/{workspace_id}")
@handle_api_exceptions("get workspace tracking status", logger)
async def get_workspace_tracking_endpoint(
    workspace_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.autonomy.tracking import get_tracking_status

    owner_id, can_edit = await _resolve_tracking_workspace_owner(
        workspace_id,
        user_id,
        require_write=False,
    )
    try:
        res = await get_tracking_status(owner_id, workspace_id)
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    is_not_applicable = res.get("status") == "not_applicable"
    return {**res, "can_edit": False if is_not_applicable else can_edit}


@router.post("/tracking/{workspace_id}")
@handle_api_exceptions("update workspace tracking status", logger)
async def update_workspace_tracking_endpoint(
    workspace_id: str,
    body: WorkspaceTrackingUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.autonomy.tracking import disable_tracking, enable_tracking

    owner_id, _ = await _resolve_tracking_workspace_owner(
        workspace_id,
        user_id,
        require_write=True,
    )
    try:
        if body.enabled:
            res = await enable_tracking(
                owner_id,
                workspace_id,
                tier=body.tier,
                force=True,
            )
        else:
            res = await disable_tracking(owner_id, workspace_id)
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**res, "can_edit": True}


# ---------------------------------------------------------------------------
# Unified System Jobs (`/api/v1/dalton/system-jobs`)
# ---------------------------------------------------------------------------


async def _authorize_system_job_mutation(key: str, user_id: str) -> str:
    """Resolve effective owner ``user_id`` for company jobs; Home/host/ops permissions are enforced in ``system_jobs``."""
    _require_dalton_enabled()
    from src.dalton.autonomy.system_jobs import (
        _resolve_company_workspace_id,
        classify_system_job_key,
    )

    try:
        scope, target = classify_system_job_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if scope in ("ops", "home", "host"):
        return str(user_id).strip()

    ws_id = await _resolve_company_workspace_id(user_id, target)
    owner_id, _ = await _resolve_tracking_workspace_owner(
        ws_id,
        user_id,
        require_write=True,
    )
    return owner_id


@router.get("/system-jobs")
@handle_api_exceptions("list dalton system jobs", logger)
async def list_system_jobs_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.autonomy.system_jobs import list_system_jobs

    try:
        return await list_system_jobs(str(user_id).strip())
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/system-jobs/{key}")
@handle_api_exceptions("toggle dalton system job", logger)
async def toggle_system_job_endpoint(
    key: str,
    body: SystemJobToggleRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.autonomy.system_jobs import toggle_system_job

    owner_id = await _authorize_system_job_mutation(key, user_id)
    try:
        return await toggle_system_job(
            owner_id,
            key,
            enabled=body.enabled,
            force=True,
            respect_user_pause=False,
        )
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/system-jobs/{key}/run")
@handle_api_exceptions("run dalton system job now", logger)
async def run_system_job_now_endpoint(
    key: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.autonomy.schedule import AutomationLockError
    from src.dalton.autonomy.system_jobs import run_system_job_now

    owner_id = await _authorize_system_job_mutation(key, user_id)
    try:
        return await run_system_job_now(owner_id, key)
    except AutomationLockError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Per-User Coverage & Earnings Calendar (`/api/v1/dalton/coverage*`, `/api/v1/dalton/earnings/calendar*`)
# ---------------------------------------------------------------------------


def _parse_tickers_payload(tickers: list[str] | str) -> list[str]:
    if isinstance(tickers, str):
        return [part.strip() for part in tickers.replace(";", ",").split(",") if part.strip()]
    out: list[str] = []
    for item in tickers or []:
        if isinstance(item, str):
            for sub in item.replace(";", ",").split(","):
                if sub.strip():
                    out.append(sub.strip())
    return out


@router.get("/coverage")
@handle_api_exceptions("get user coverage", logger)
async def get_user_coverage_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import list_user_coverage_summary

    return await list_user_coverage_summary(str(user_id).strip())


@router.post("/coverage")
@handle_api_exceptions("mutate user coverage", logger)
async def mutate_user_coverage_endpoint(
    body: CoverageMutateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import (
        CoverageDataSourceError,
        add_user_coverage_tickers,
        remove_user_coverage_tickers,
    )

    uid = str(user_id).strip()
    act = str(body.action or "").strip().lower()
    tickers = _parse_tickers_payload(body.tickers)
    if not tickers:
        raise HTTPException(status_code=400, detail="tickers is required")

    try:
        if act == "add":
            return await add_user_coverage_tickers(
                uid,
                tickers,
                tier=body.tier or "watch",
                source="ui",
            )
        if act == "remove":
            return await remove_user_coverage_tickers(uid, tickers)
        if act == "set_tier":
            from src.dalton.autonomy.tracking import set_tier_and_resync

            return await set_tier_and_resync(uid, tickers, tier=body.tier or "watch")
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported action {body.action!r}; must be 'add', 'remove', or 'set_tier'",
    )


@router.get("/coverage/groups")
@handle_api_exceptions("list user coverage groups", logger)
async def list_user_coverage_groups_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import list_user_coverage_summary

    summary = await list_user_coverage_summary(str(user_id).strip())
    return {
        "ok": True,
        "user_id": str(user_id).strip(),
        "groups": summary.get("groups") or [],
    }


@router.post("/coverage/groups/preview")
@handle_api_exceptions("preview user coverage group", logger)
async def preview_user_coverage_group_endpoint(
    body: CoverageGroupPreviewRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import CoverageDataSourceError, preview_coverage_group

    _ = user_id
    try:
        return await preview_coverage_group(
            label=body.label,
            criteria=body.criteria,
        )
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/coverage/groups")
@handle_api_exceptions("create user coverage group", logger)
async def create_user_coverage_group_endpoint(
    body: CoverageGroupCreateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import CoverageDataSourceError, add_coverage_group

    try:
        return await add_coverage_group(
            str(user_id).strip(),
            label=body.label,
            criteria=body.criteria,
            default_tier=body.default_tier,
            auto_refresh=body.auto_refresh,
        )
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/coverage/groups/{group_id}")
@handle_api_exceptions("delete user coverage group", logger)
async def delete_user_coverage_group_endpoint(
    group_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import remove_coverage_group

    res = await remove_coverage_group(str(user_id).strip(), group_id)
    if not res.get("ok") or res.get("deleted") is False:
        raise HTTPException(status_code=404, detail=str(res.get("error") or "Group not found"))
    return res


@router.post("/coverage/groups/{group_id}/resolve")
@handle_api_exceptions("resolve user coverage group", logger)
async def resolve_user_coverage_group_endpoint(
    group_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import CoverageDataSourceError, resolve_coverage_group

    try:
        res = await resolve_coverage_group(str(user_id).strip(), group_id)
    except CoverageDataSourceError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=str(res.get("error") or "Group not found"))
    return res


@router.get("/earnings/calendar")
@handle_api_exceptions("get user earnings calendar", logger)
async def get_user_earnings_calendar_endpoint(
    user_id: CurrentUserId,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import query_user_earnings_calendar

    return await query_user_earnings_calendar(
        str(user_id).strip(),
        from_date=from_date,
        to_date=to_date,
    )


@router.put("/earnings/calendar/mirror")
@handle_api_exceptions("publish user earnings calendar mirror", logger)
async def put_user_earnings_calendar_mirror_endpoint(
    body: EarningsCalendarMirrorRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    _require_dalton_enabled()
    from src.dalton.coverage import publish_user_earnings_calendar_mirror

    if isinstance(body.ledger, dict):
        ledger_payload = body.ledger
    else:
        dumped = body.model_dump(exclude={"ledger"}, exclude_none=True)
        ledger_payload = dumped

    try:
        return await publish_user_earnings_calendar_mirror(
            str(user_id).strip(),
            ledger_payload,
        )
    except ValueError as exc:
        msg = str(exc)
        if "1 MB" in msg or "exceeds" in msg:
            raise HTTPException(status_code=413, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc


class InviteCheckRequest(BaseModel):
    token: str


class InviteRedeemRequest(BaseModel):
    token: str
    username: str
    password: str


@router.post("/invites")
@handle_api_exceptions("create dalton invite", logger)
async def create_invite_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.invites import create_invite_for_pm

    return await create_invite_for_pm(user_id)


@router.get("/invites")
@handle_api_exceptions("list dalton invites", logger)
async def list_invites_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.invites import MAX_ACTIVE_INVITES_PER_USER, list_invites_for_pm

    invites = await list_invites_for_pm(user_id)
    active_count = sum(1 for item in invites if item.get("status") == "pending")
    return {
        "invites": invites,
        "active_count": active_count,
        "max_active": MAX_ACTIVE_INVITES_PER_USER,
    }


@router.delete("/invites/{invite_id}")
@handle_api_exceptions("revoke dalton invite", logger)
async def revoke_invite_endpoint(
    invite_id: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.invites import revoke_invite_for_pm

    return await revoke_invite_for_pm(user_id, invite_id)


@router.post("/invites/check")
@handle_api_exceptions("check dalton invite token", logger)
async def check_invite_endpoint(
    body: InviteCheckRequest,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    from src.dalton.invites import check_invite_token, enforce_invite_rate_limit

    await enforce_invite_rate_limit(request, action="check")
    return await check_invite_token(body.token)


@router.post("/invites/redeem")
@handle_api_exceptions("redeem dalton invite token", logger)
async def redeem_invite_endpoint(
    body: InviteRedeemRequest,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    from src.dalton.invites import enforce_invite_rate_limit, redeem_invite_token

    await enforce_invite_rate_limit(request, action="redeem")
    return await redeem_invite_token(
        token=body.token,
        username=body.username,
        password=body.password,
    )


# ---------------------------------------------------------------------------
# Feishu doc read scopes (DALTON-HOOK(feishu-docs)): admin-maintained whitelist
# of wiki spaces external members may read via the feishu_* agent tools.
# ---------------------------------------------------------------------------


class FeishuScopeEntry(BaseModel):
    """One whitelist entry; validation failures surface as HTTP 422 at the request boundary."""

    space_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        from src.dalton.database.feishu import normalize_space_id

        try:
            return normalize_space_id(value)
        except ValueError as exc:
            raise ValueError(
                "space_id 只允许字母、数字、下划线和连字符（去除首尾空白后不能为空）"
            ) from exc

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()


class FeishuReadScopesUpdateRequest(BaseModel):
    scopes: list[FeishuScopeEntry] = Field(max_length=200)


def _require_feishu_admin(user_id: str | None) -> str:
    """Require a PM user (`DALTON_PM_USER_IDS`) for read-scope and space-list management."""
    _require_dalton_enabled()
    from src.dalton.invites import is_invite_pm

    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not is_invite_pm(uid):
        raise HTTPException(status_code=403, detail="仅管理员可管理飞书读取范围")
    return uid


@router.get("/feishu/read-scopes")
@handle_api_exceptions("list dalton feishu read scopes", logger)
async def list_feishu_read_scopes_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.database.feishu import list_read_scopes

    _require_feishu_admin(user_id)
    return {"scopes": await list_read_scopes()}


@router.put("/feishu/read-scopes")
@handle_api_exceptions("update dalton feishu read scopes", logger)
async def update_feishu_read_scopes_endpoint(
    body: FeishuReadScopesUpdateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.database.feishu import replace_read_scopes

    uid = _require_feishu_admin(user_id)
    try:
        scopes = await replace_read_scopes(
            [entry.model_dump() for entry in body.scopes],
            added_by=uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"scopes": scopes}


@router.get("/feishu/spaces")
@handle_api_exceptions("list dalton feishu wiki spaces", logger)
async def list_feishu_spaces_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    from src.dalton.feishu import FeishuBridgeError, post_bridge_feishu

    _require_feishu_admin(user_id)
    try:
        res = await post_bridge_feishu("wiki/nodes", {"space_id": ""})
    except FeishuBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if res.get("ok") is not True:
        detail = str(res.get("error") or "feishu_error")
        raise HTTPException(status_code=502, detail=detail)
    return {"spaces": res.get("spaces") or []}

@router.get("/search")
@handle_api_exceptions("global search", logger)
async def global_search_endpoint(
    user_id: CurrentUserId,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    # Known limitation: when object storage is enabled for workspace_files,
    # file content is stored in external blobs rather than workspace_files.content_text
    # (which is NULL in Postgres). In that case workspace_files are matched by
    # file_name and file_path only.
    _require_dalton_enabled()
    cleaned_q = (q or "").strip()
    if len(cleaned_q) < 2:
        raise HTTPException(
            status_code=422,
            detail="Search query 'q' must be at least 2 characters after trimming whitespace.",
        )
    from src.dalton.database.search import global_search

    return await global_search(user_id, cleaned_q, limit=limit)




