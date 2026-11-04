"""The owner's side of share links: mint, list, stop.

A file is shared by publishing its artifact (``app/artifacts``), which cuts
the version visitors see; these routes find a file's or an app's link and
stop one, and never share.

Endpoints, all owner-only:
- POST  /api/v1/workspaces/{id}/file-grant          - a path grant for the owner's viewer
- POST  /api/v1/workspaces/{id}/share-links         - get or create an item's link (file or app)
- GET   /api/v1/workspaces/{id}/share-links         - the shared links here
- PATCH /api/v1/workspaces/{id}/share-links/{code}  - stop sharing
- GET   /api/v1/share-links/{code}                  - one of the owner's links, any workspace
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.server.app.artifacts.records import artifact_path
from src.server.database import artifacts as artifacts_db
from src.server.database.share_codes import share_path
from src.server.database.share_links import (
    KIND_APP,
    KIND_FILE,
    ShareLink,
    ensure_app_link,
    file_link,
    get_link,
    list_shared,
    set_private,
)
from src.server.database.workspace import get_workspace as db_get_workspace
from src.server.services.artifacts.events import publish_artifact_change
from src.server.services.file_grants import grant_prefix, mint_file_grant, seconds_left
from src.server.services.workspace_files import owned_workspace_on_computer
from src.dalton.access_tiers import get_user_tier as _default_get_user_tier
from src.dalton.settings import is_dalton_supabase_auth_enabled
from src.server.utils.api import CurrentUserId

# DALTON-HOOK(external-share-guard): external members cannot make a file
# public. #666 moved the enable side of this gate onto the artifact routes
# (`app/artifacts/records.py`: publish and set keep_updated), which import
# this helper; stopping a share (below) stays open to everyone.
EXTERNAL_SHARE_FORBIDDEN_DETAIL = "外部成员不能创建公开分享链接"
get_user_tier = _default_get_user_tier


async def require_non_external_for_share(user_id: str | None) -> None:
    """Reject external members from making a file publicly reachable."""
    from src.dalton import access_tiers as _access_tiers
    from src.dalton import sharing as _dalton_sharing

    if get_user_tier is not _default_get_user_tier:
        tier_fn = get_user_tier
    elif _access_tiers.get_user_tier is not _default_get_user_tier:
        tier_fn = _access_tiers.get_user_tier
    elif _dalton_sharing.get_user_tier is not _default_get_user_tier:
        tier_fn = _dalton_sharing.get_user_tier
    else:
        tier_fn = _default_get_user_tier
    if not is_dalton_supabase_auth_enabled() and tier_fn is _default_get_user_tier:
        return
    if await tier_fn(user_id) == "external":
        raise HTTPException(
            status_code=403,
            detail=EXTERNAL_SHARE_FORBIDDEN_DETAIL,
        )

router = APIRouter(prefix="/api/v1/workspaces", tags=["Share Links"])
owner_router = APIRouter(prefix="/api/v1/share-links", tags=["Share Links"])


class ShareLinkCreate(BaseModel):
    kind: Literal["file", "app"]
    path: str | None = None
    port: int | None = Field(None, ge=3000, le=9999)


class ShareLinkUpdate(BaseModel):
    shared: bool


class ShareLinkResponse(BaseModel):
    code: str
    workspace_id: str
    url: str
    kind: str
    path: str | None
    port: int | None
    #: A file's artifact, so an ``/a/`` link in a reply opens it.
    artifact_id: str | None
    title: str | None
    shared: bool
    shared_at: datetime | None
    shared_files: list[str] | None
    created_at: datetime


class FileGrantResponse(BaseModel):
    prefix: str
    expires_in: int


def _to_model(link: ShareLink) -> ShareLinkResponse:
    return ShareLinkResponse(
        code=link.code,
        workspace_id=link.workspace_id,
        url=share_path(link.code),
        kind=link.kind,
        path=link.path,
        port=link.port,
        artifact_id=link.artifact_id,
        title=link.display_title,
        shared=link.shared,
        shared_at=link.shared_at,
        shared_files=list(link.files) if link.shared and link.files else None,
        created_at=link.created_at,
    )


async def _owned_link(workspace: dict[str, Any], code: str) -> ShareLink:
    link = await get_link(code)
    if link is None or link.workspace_id != str(workspace["workspace_id"]):
        raise HTTPException(status_code=404, detail="Share link not found")
    return link


@router.post("/{workspace_id}/file-grant", response_model=FileGrantResponse)
async def mint_workspace_file_grant(
    workspace_id: str, x_user_id: CurrentUserId
) -> FileGrantResponse:
    """A path grant for the owner's own viewers; see ``services/file_grants``."""
    workspace = await owned_workspace_on_computer(workspace_id, x_user_id)
    grant = await mint_file_grant(str(workspace["workspace_id"]))
    return FileGrantResponse(
        prefix=grant_prefix(grant), expires_in=seconds_left(grant.expires_at)
    )


@router.post("/{workspace_id}/share-links", response_model=ShareLinkResponse)
async def get_or_create_share_link(
    workspace_id: str, body: ShareLinkCreate, x_user_id: CurrentUserId
) -> ShareLinkResponse:
    """The item's one link, private on first sight. A file's is its artifact
    record, so this is also how a file gets one before a comment or a publish."""
    # DALTON-HOOK(external-share-guard): no gate here — the link starts
    # private; making a file public is publish (or keep_updated), which the
    # artifact routes guard.
    workspace = await owned_workspace_on_computer(workspace_id, x_user_id)

    if body.kind == KIND_APP:
        if body.port is None:
            raise HTTPException(status_code=422, detail="A port is required")
        return _to_model(
            await ensure_app_link(str(workspace["workspace_id"]), body.port)
        )

    ws_id, path = artifact_path(workspace, body.path or "")
    return _to_model(file_link(await artifacts_db.ensure_artifact(ws_id, path)))


@router.get("/{workspace_id}/share-links")
async def list_share_links(
    workspace_id: str, x_user_id: CurrentUserId
) -> dict[str, list[ShareLinkResponse]]:
    """Only shared links: a private one is found through its item."""
    workspace = await owned_workspace_on_computer(workspace_id, x_user_id)
    links = await list_shared(str(workspace["workspace_id"]))
    return {"links": [_to_model(link) for link in links]}


@router.patch("/{workspace_id}/share-links/{code}", response_model=ShareLinkResponse)
async def update_share_link(
    workspace_id: str, code: str, body: ShareLinkUpdate, x_user_id: CurrentUserId
) -> ShareLinkResponse:
    """Stop sharing. A file is shared by publishing its artifact, which cuts
    the version visitors see, so asking to share one here is a 409 naming the
    artifact to publish; an app link is never shared.
    """
    workspace = await owned_workspace_on_computer(workspace_id, x_user_id)
    link = await _owned_link(workspace, code)

    if link.kind == KIND_FILE:
        if body.shared:
            raise HTTPException(
                status_code=409,
                detail={"code": "publish_required", "artifact_id": link.artifact_id},
            )
        try:
            artifact = await artifacts_db.unshare(
                user_id=x_user_id, artifact_id=link.artifact_id or ""
            )
        except artifacts_db.ArtifactNotFound:
            raise HTTPException(status_code=404, detail="Share link not found") from None
        await publish_artifact_change(x_user_id, artifact)
        return _to_model(file_link(artifact))

    # DALTON-HOOK(external-share-guard): no gate on the app branch either —
    # an app link can never be shared, and stopping one is all this does.
    if body.shared:
        raise HTTPException(status_code=400, detail="An app link cannot be shared")
    return _to_model(await set_private(code) or link)


@owner_router.get("/{code}", response_model=ShareLinkResponse)
async def get_share_link(code: str, x_user_id: CurrentUserId) -> ShareLinkResponse:
    """What one of the owner's links points at, so their app can open it in place.

    The workspace list holds shared links only, and an app link is never shared,
    so an ``/a/`` link in a reply is mapped back to its port or file through
    this. Any of the owner's workspaces answers, since a report-back carries an
    analyst's links into Home; the link names the workspace to open it in.
    Another user's code is not found rather than forbidden.
    """
    link = await get_link(code)
    workspace = await db_get_workspace(link.workspace_id) if link else None
    if link is None or workspace is None or workspace.get("user_id") != x_user_id:
        raise HTTPException(status_code=404, detail="Share link not found")
    return _to_model(link)
