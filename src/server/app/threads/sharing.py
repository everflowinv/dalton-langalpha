"""Public thread sharing: toggle share state and read share status.

A shared workspace's thread is shared by its members, as its owner shares a
personal one, as far as the workspace's managers allow
(``shared_workspaces.share_out``); who its link may open to is judged by the
holder's organization, as its visitors are (members of it only, never
anyone)."""

import json
from datetime import datetime, timezone


from fastapi import HTTPException

from src.server.utils import api as auth_api
from src.server.utils.api import (
    CurrentUserId,
)
from src.server.models.conversation import (
    ThreadShareRequest,
    ThreadShareResponse,
    SharePermissions,
)
from src.dalton.access_tiers import get_user_tier as _default_get_user_tier
from src.dalton.settings import is_dalton_supabase_auth_enabled
from src.server.database.conversation import (
    get_thread_by_id,
    update_thread_sharing,
)
from src.server.services import activity
from src.server.services.activity import Action
from src.server.services.share_audience import (
    ORGANIZATION,
    offered,
    refuse_public_link_of,
    require_members_audience,
    thread_audience,
)
from src.server.services.shared_workspaces import access as shared_access
from src.server.services.shared_workspaces.share_out import refuse_link_change

# DALTON-HOOK(external-share-guard): external members cannot enable public thread sharing
EXTERNAL_SHARE_FORBIDDEN_DETAIL = "外部成员不能创建公开分享链接"
get_user_tier = _default_get_user_tier


async def _require_non_external_for_share(user_id: str | None) -> None:
    """Reject external members from enabling public thread sharing."""
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


# DALTON-HOOK(monitors): enabling public thread sharing with workspace files
# exposes non-public monitor source files; require administrator privilege.
PUBLIC_THREAD_FILES_FORBIDDEN_DETAIL = "仅管理员可开启附带工作区文件的公开对话分享"


async def _require_admin_for_share_files(user_id: str | None) -> None:
    """Reject non-admin users from enabling public thread sharing with workspace file access."""
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
    if await tier_fn(user_id) != "admin":
        raise HTTPException(
            status_code=403,
            detail=PUBLIC_THREAD_FILES_FORBIDDEN_DETAIL,
        )



from ._deps import router


# =============================================================================
# THREAD SHARING
# =============================================================================


def _perms(perms) -> dict:
    if isinstance(perms, str):
        perms = json.loads(perms)
    return perms if isinstance(perms, dict) else {}


def _share_flags(perms) -> dict[str, bool]:
    """What a link lets a visitor do, its yes-or-no permissions only."""
    return {k: v for k, v in _perms(perms).items() if isinstance(v, bool)}


def _record_share_change(
    user_id: str, thread_id: str, thread: dict, updated: dict
) -> None:
    """Record what the request changed about the thread's link: turned on,
    turned off, or, on a link already on, which permissions moved. Asking
    for what already stands records nothing."""
    was_on, is_on = bool(thread.get("is_shared")), bool(updated.get("is_shared"))
    after = _share_flags(updated.get("share_permissions"))
    if is_on and not was_on:
        action, detail = Action.THREAD_LINK_ON, after
        if thread_audience(updated.get("share_permissions")) == ORGANIZATION:
            detail = {**after, "audience": ORGANIZATION}
    elif was_on and not is_on:
        action, detail = Action.THREAD_LINK_OFF, after
    elif is_on:
        before_perms = _perms(thread.get("share_permissions"))
        after_perms = _perms(updated.get("share_permissions"))
        before = _share_flags(before_perms)
        detail = {k: v for k, v in after.items() if before.get(k, False) != v}
        # The folder a link opens is a path, so only that it moved is kept.
        if before_perms.get("root_path") != after_perms.get("root_path"):
            detail["root_path_changed"] = True
        if thread_audience(before_perms) != thread_audience(after_perms):
            detail["audience"] = thread_audience(after_perms)
        if not detail:
            return
        action = Action.THREAD_LINK_PERMISSIONS_CHANGED
    else:
        return
    activity.record(
        user_id, action, resource=("thread", thread_id), thread_id=thread_id,
        workspace_id=updated.get("workspace_id") or thread.get("workspace_id"),
        detail=detail,
    )


async def _authorize_thread_share(thread_id: str, user_id: str):
    """Bridge Dalton thread ownership checks and upstream shared-workspace access."""
    # --- DALTON-HOOK(colleague-dispatch): begin ---
    from src.server.database.shared_workspaces import is_holder
    from src.server.services.membership import teams_door_open
    from src.server.services.shared_workspaces.access import Access

    if not is_dalton_supabase_auth_enabled():
        return await shared_access.thread_access(thread_id, user_id)
    if teams_door_open():
        from src.server.database.conversation import get_thread_auth_meta

        meta = await get_thread_auth_meta(thread_id)
        if meta is not None and is_holder(meta["user_id"]):
            return await shared_access.thread_access(thread_id, user_id, meta=meta)
    await auth_api.require_thread_owner(thread_id, user_id)
    return Access(user_id)
    # --- DALTON-HOOK(colleague-dispatch): end ---


@router.post("/{thread_id}/share", response_model=ThreadShareResponse)
async def update_thread_share(
    thread_id: str,
    request: ThreadShareRequest,
    x_user_id: CurrentUserId,
):
    """Toggle sharing for a thread and update permissions and audience.

    ``public_links`` governs only a link that opens to anyone; the members
    audience is asked of the owner's account instead, and only when chosen.
    """
    access = await _authorize_thread_share(thread_id, x_user_id)
    owner_id = access.owner_id
    await refuse_link_change(access)

    thread = await get_thread_by_id(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    chosen = (
        request.permissions.audience
        if request.permissions is not None
        and "audience" in request.permissions.model_fields_set
        else thread_audience(_perms(thread.get("share_permissions")))
    )
    if chosen == ORGANIZATION:
        if request.permissions is not None and "audience" in request.permissions.model_fields_set:
            await require_members_audience(owner_id)
    elif request.is_shared:
        await refuse_public_link_of(owner_id)

    # DALTON-HOOK(colleague-dispatch): public thread sharing requires owning both thread and workspace
    if is_dalton_supabase_auth_enabled():
        ws_owner = thread.get("workspace_owner_id")
        if ws_owner and str(ws_owner) != str(x_user_id):
            raise HTTPException(status_code=403, detail="Forbidden")

    # Build update kwargs
    kwargs: dict = {"is_shared": request.is_shared}

    if request.is_shared:
        # DALTON-HOOK(external-share-guard): external members cannot enable public thread sharing
        await _require_non_external_for_share(x_user_id)
        kwargs["shared_at"] = datetime.now(timezone.utc)

    # Merge permissions: start from existing, overlay provided fields
    existing_perms = thread.get("share_permissions") or {}
    if isinstance(existing_perms, str):
        existing_perms = json.loads(existing_perms)

    if request.permissions is not None:
        # ``exclude_unset``: a request that speaks only about the flags must not
        # silently widen a token scoped to one report back to the whole
        # workspace, which a defaulted ``root_path`` of "" would do.
        merged = {**existing_perms, **request.permissions.model_dump(exclude_unset=True)}
        # Enforce: download requires files
        if merged.get("allow_download") and not merged.get("allow_files"):
            merged["allow_files"] = True
        kwargs["share_permissions"] = merged
        effective_perms = merged
    else:
        effective_perms = existing_perms

    # DALTON-HOOK(monitors): block public shared-thread file exposure by non-admins
    if request.is_shared and (
        effective_perms.get("allow_files") or effective_perms.get("allow_download")
    ):
        await _require_admin_for_share_files(x_user_id)

    updated = await update_thread_sharing(thread_id, **kwargs)
    if not updated:
        raise HTTPException(status_code=404, detail="Thread not found")
    _record_share_change(x_user_id, thread_id, thread, updated)

    share_token = updated.get("share_token")
    perms = updated.get("share_permissions") or {}
    if isinstance(perms, str):
        perms = json.loads(perms)

    return ThreadShareResponse(
        is_shared=updated["is_shared"],
        share_token=share_token if updated["is_shared"] else None,
        share_url=f"/s/{share_token}" if updated["is_shared"] and share_token else None,
        permissions=SharePermissions(**(perms if isinstance(perms, dict) else {})),
        audiences=await offered(owner_id, thread_audience(_perms(perms))),
    )


@router.get("/{thread_id}/share", response_model=ThreadShareResponse)
async def get_thread_share(thread_id: str, x_user_id: CurrentUserId):
    """Get current share status and permissions for a thread."""
    owner_id = (await _authorize_thread_share(thread_id, x_user_id)).owner_id

    thread = await get_thread_by_id(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    # DALTON-HOOK(colleague-dispatch): public thread sharing requires owning both thread and workspace
    from src.dalton.settings import is_dalton_supabase_auth_enabled

    if is_dalton_supabase_auth_enabled():
        ws_owner = thread.get("workspace_owner_id")
        if ws_owner and str(ws_owner) != str(x_user_id):
            raise HTTPException(status_code=403, detail="Forbidden")

    share_token = thread.get("share_token")
    is_shared = thread.get("is_shared", False)
    perms = thread.get("share_permissions") or {}
    if isinstance(perms, str):
        perms = json.loads(perms)

    return ThreadShareResponse(
        is_shared=is_shared,
        share_token=share_token if is_shared else None,
        share_url=f"/s/{share_token}" if is_shared and share_token else None,
        permissions=SharePermissions(**(perms if isinstance(perms, dict) else {})),
        audiences=await offered(owner_id, thread_audience(_perms(perms))),
    )
