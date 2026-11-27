"""Core service layer for Dalton Monitors: publish, version cutting, ACLs, auto-update, serving, and data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from psycopg.rows import dict_row

from src.config.env import PREVIEW_PROXY_FRAME_ANCESTORS
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth_guards import (
    ADMIN_REQUIRED_CODE,
    forbidden_detail,
    raise_admin_required,
    raise_external_member_forbidden,
)
from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID, is_company_system_user
from src.dalton.monitors import db as monitor_db
from src.dalton.monitors.data_job_registry import (
    HostDirFileSource,
    MonitorDataJobBinding,
    ScopedMountFileSource,
    get_registered_monitor_binding_by_slug,
    get_registered_monitor_bindings,
)
from src.dalton.monitors.grants import (
    MonitorGrantError,
    data_prefix,
    frame_prefix,
    is_shared_document_grant,
    mint_monitor_grant,
    verify_monitor_grant,
)
from src.dalton.monitors.models import (
    CompanyMember,
    MonitorDataEntry,
    MonitorRecord,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
    entry_file_type_of,
    is_html_entry_path,
    is_monitor_panel_entry,
    is_valid_data_key,
    is_valid_slug,
)
from src.dalton.monitors.permissions import (
    EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE,
    can_manage_monitor_fields,
    can_view_monitor,
    is_monitor_capability_enabled,
    require_monitor_capability,
    require_monitor_publish_visibility,
    require_monitor_view,
    validate_publish_permissions,
)
from src.dalton.settings import is_artifact_publishing_enabled
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import VersionFile
from src.server.database.artifacts.versions import (
    LeftOutChanged,
    Origin,
    PublishResult,
    ReadBasis,
    SharingChanged,
    VersionMoved,
)
from src.server.database.workspace import get_workspace as db_get_workspace
from src.server.database.workspace_file_blobs import fetch_blob
from src.server.preview_proxy.artifacts import (
    ArtifactFileUnavailable,
    ServedArtifactFile,
    _charset_is_utf8,
    _content_type,
    _etag_matches,
    _headers,
    _is_html,
    _valid_path,
)
from src.server.preview_proxy.page_bridge import bridge_for, with_bridge
from src.server.services.artifacts import snapshot as artifact_snapshot
from src.server.services.artifacts.manifest import REASON_LOADED
from src.server.services.artifacts.reader import ReadMode, Reader
from src.server.services.file_grants import seconds_left
from src.server.services.served_paths import may_be_artifact
from src.server.services.workspace_file_containment import contained_relative_path
from src.server.services.workspace_files import (
    owned_workspace_on_computer,
    owner_work_dir,
    previous_dir_names_of,
)

logger = logging.getLogger(__name__)
_REAL_GET_USER_TIER = get_user_tier


def _require_publishing_enabled() -> None:
    if not is_artifact_publishing_enabled():
        raise HTTPException(
            status_code=403,
            detail={"code": "artifact_publishing_disabled"},
        )


def _normalize_workspace_entry_path(workspace: dict[str, Any], raw_path: str) -> str:
    """Resolve a workspace HTML file or folder path (defaulting folders to ``index.html``)."""
    work_dir = owner_work_dir(workspace)
    prev_dirs = previous_dir_names_of(workspace)
    clean = str(raw_path or "").strip().rstrip("/")
    relative = contained_relative_path(clean, work_dir, prev_dirs)
    if relative is None:
        raise HTTPException(status_code=400, detail="无效的工作区文件路径")
    last_segment = relative.rsplit("/", 1)[-1]
    if not may_be_artifact(relative) or "." not in last_segment:
        candidate = f"{relative.rstrip('/')}/index.html"
        if may_be_artifact(candidate):
            return candidate
    if not may_be_artifact(relative):
        raise HTTPException(status_code=400, detail="该文件不能发布为监控面板")
    if not is_html_entry_path(relative):
        raise HTTPException(status_code=400, detail="监控面板仅支持发布 HTML 网页文件")
    return relative


PUBLIC_PERMISSION_LOST_MESSAGE = "发布人已无公开权限"


class PublicAuthorityLostError(PermissionError):
    """Raised when a public monitor's publisher or approver is no longer an admin."""


def _can_manage_monitor(monitor: MonitorRecord, user_id: str, tier: str) -> bool:
    return can_manage_monitor_fields(
        owner_user_id=monitor.owner_user_id,
        visibility=monitor.visibility,
        caller_id=user_id,
        caller_tier=tier,
    )


async def _resolve_workspace_owner_id(monitor: MonitorRecord) -> str:
    if monitor.workspace_id:
        ws = await db_get_workspace(monitor.workspace_id)
        if ws and ws.get("user_id"):
            return str(ws["user_id"])
    return monitor.owner_user_id


async def _verify_user_is_admin(user_id: str, *, conn: Any = None) -> None:
    tier = await monitor_db._call_with_optional_conn(
        get_user_tier, user_id, conn=conn
    )
    if tier != "admin":
        raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)


async def _abort_if_absent_dependencies(
    artifact_id: str,
    prev_seq: int,
    snap: artifact_snapshot.Snapshot,
) -> None:
    if prev_seq <= 0 or not snap.absent:
        return
    prev_hashes = await artifacts_db.get_version_hashes(artifact_id, prev_seq)
    missing = sorted(set(snap.absent) & set(prev_hashes))
    if missing:
        raise RuntimeError(
            f"缺少上一版依赖文件，已保留上一版: {', '.join(missing)}"
        )


async def cut_monitor_version(
    *,
    monitor: MonitorRecord,
    owner_user_id: str,
    files: list[VersionFile],
    title: str | None,
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    run_id: str | None = None,
    artifact: Any | None = None,
    new_owner_user_id: str | None = None,
    caller_user_id: str | None = None,
    verify_public_authority: Any = None,
    check_pending_outbound_confirm: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
) -> tuple[MonitorRecord, PublishResult | None, bool]:
    """Cut a monitor version atomically with visibility lock re-read, unshare, and run_id dedup."""
    _ = artifact
    return await monitor_db.commit_monitor_version_tx(
        monitor_id=monitor.monitor_id,
        artifact_id=monitor.artifact_id,
        workspace_owner_id=owner_user_id,
        files=files,
        title=title or monitor.title,
        label=label,
        read_basis=read_basis,
        origin=origin,
        loaded_files=loaded_files,
        left_out_files=left_out_files,
        run_id=run_id,
        new_owner_user_id=new_owner_user_id,
        caller_user_id=caller_user_id,
        verify_public_authority=verify_public_authority,
        check_pending_outbound_confirm=check_pending_outbound_confirm,
        reject_if_pending_outbound_confirm=reject_if_pending_outbound_confirm,
    )


def _translate_snapshot_error(exc: Exception) -> HTTPException:
    from src.dalton.colleague_outbound_guard import (
        PENDING_CONFIRM_REQUIRED_MESSAGE,
        WorkspaceAutoPublishPaused,
    )

    if isinstance(exc, WorkspaceAutoPublishPaused):
        return HTTPException(
            status_code=409,
            detail={
                "code": "pending_outbound_confirm",
                "message": PENDING_CONFIRM_REQUIRED_MESSAGE,
            },
        )
    if isinstance(exc, artifact_snapshot.SnapshotRefused):
        if exc.code == "file_missing":
            return HTTPException(status_code=404, detail={"code": "file_missing"})
        if exc.code == "files_changed":
            return HTTPException(
                status_code=409,
                detail={
                    "code": "files_changed",
                    "files": [entry.as_dict() for entry in exc.files or ()],
                },
            )
        if exc.code == "needs_computer":
            return HTTPException(status_code=409, detail={"code": "needs_computer"})
        return HTTPException(
            status_code=422, detail={"code": exc.code, "limit": exc.limit}
        )
    if isinstance(exc, artifact_snapshot.SnapshotUnavailable):
        return HTTPException(
            status_code=503,
            detail="文件暂不可读取，请稍后重试。",
        )
    if isinstance(exc, LeftOutChanged):
        return HTTPException(status_code=409, detail={"code": "files_changed"})
    if isinstance(exc, SharingChanged):
        return HTTPException(status_code=409, detail={"code": "sharing_changed"})
    if isinstance(exc, VersionMoved):
        return HTTPException(status_code=409, detail={"code": "version_moved"})
    if isinstance(exc, PublicAuthorityLostError):
        return HTTPException(
            status_code=403,
            detail=forbidden_detail(ADMIN_REQUIRED_CODE, str(exc)),
        )
    if isinstance(exc, HTTPException):
        return exc
    return HTTPException(status_code=500, detail=str(exc))


def _extract_publish_failure_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        msg = str(exc.detail.get("message") or "").strip()
        if msg:
            return msg
    return str(exc)


async def _get_superseded_shared_document(monitor: MonitorRecord) -> Any | None:
    """Return the governing ``SharedDocumentRecord`` when a legacy non-HTML monitor was taken over."""
    entry_path = getattr(monitor, "entry_path", None)
    if (
        getattr(monitor, "source_kind", None) != "workspace"
        or not entry_path
        or is_html_entry_path(entry_path)
    ):
        return None
    from src.dalton.monitors import shared_documents

    return await shared_documents.get_shared_document_by_artifact_id(
        monitor.artifact_id
    )


async def _build_monitor_detail_payload(
    monitor: MonitorRecord,
    *,
    viewer_id: str,
    read_only: bool = False,
) -> dict[str, Any]:
    versions = await monitor_db.list_monitor_versions(
        monitor.monitor_id,
        monitor.artifact_id,
        fallback_entry_path=monitor.entry_path,
    )
    grant = await mint_monitor_grant(
        monitor.monitor_id, viewer_id, monitor.acl_revision
    )
    tier = await get_user_tier(viewer_id)
    can_manage = False if read_only else _can_manage_monitor(monitor, viewer_id, tier)
    return {
        "monitor": serialize_monitor(monitor, can_manage=can_manage),
        "versions": [
            {
                "seq": v.seq,
                "entry_path": v.entry_path or monitor.entry_path,
                "origin": v.origin,
                "title": v.title,
                "label": v.label,
                "byte_len": v.byte_len,
                "published_at": v.published_at.isoformat(),
            }
            for v in versions
        ],
        "monitor_grant": grant.token,
        "frame_base": frame_prefix(grant),
        "data_base": data_prefix(grant),
        "expires_in": seconds_left(grant.expires_at),
    }


async def get_public_monitor_detail(slug: str) -> dict[str, Any]:
    """Return trimmed read-only monitor detail for an anonymous viewer when ``visibility == 'public'``."""
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if (
        monitor is None
        or monitor.visibility != "public"
        or not is_monitor_capability_enabled(monitor)
        or await _get_superseded_shared_document(monitor) is not None
    ):
        raise HTTPException(status_code=404, detail="监控面板不存在")
    serialized = serialize_monitor(monitor, can_manage=False)
    for internal_key in (
        "workspace_id",
        "artifact_id",
        "artifact_code",
        "owner_user_id",
        "entry_path",
        "source_ref",
        "last_publish_error",
        "last_publish_failed_at",
    ):
        serialized.pop(internal_key, None)
    serialized["owner_email"] = None
    serialized["member_user_ids"] = []
    return {
        "monitor": serialized,
        "versions": [],
        "monitor_grant": "",
        "frame_base": f"/api/v1/dalton/monitors/public/{monitor.slug}/",
        "data_base": f"/api/v1/dalton/monitors/public/{monitor.slug}/data",
        "expires_in": 0,
    }


def serialize_monitor(monitor: MonitorRecord, *, can_manage: bool = False) -> dict[str, Any]:
    return {
        "monitor_id": monitor.monitor_id,
        "slug": monitor.slug,
        "artifact_id": monitor.artifact_id,
        "artifact_code": monitor.artifact_code,
        "workspace_id": monitor.workspace_id,
        "entry_path": monitor.entry_path,
        "entry_file_type": entry_file_type_of(monitor.entry_path),
        "owner_user_id": monitor.owner_user_id,
        "owner_name": monitor.owner_name,
        "owner_email": monitor.owner_email,
        "title": monitor.title,
        "category": monitor.category,
        "sort_order": monitor.sort_order,
        "visibility": monitor.visibility,
        "member_user_ids": list(monitor.member_user_ids),
        "published_seq": monitor.published_seq,
        "keep_updated": monitor.keep_updated,
        "source_kind": monitor.source_kind,
        "source_ref": monitor.source_ref,
        "acl_revision": monitor.acl_revision,
        "last_publish_error": monitor.last_publish_error,
        "last_publish_failed_at": (
            monitor.last_publish_failed_at.isoformat()
            if monitor.last_publish_failed_at
            else None
        ),
        "data_as_of": monitor.data_as_of.isoformat() if monitor.data_as_of else None,
        "created_at": monitor.created_at.isoformat(),
        "updated_at": monitor.updated_at.isoformat(),
        "can_manage": can_manage,
    }


async def list_monitors_for_user(user_id: str) -> list[dict[str, Any]]:
    tier = await get_user_tier(user_id)
    rows = await monitor_db.list_all_monitors()
    visible: list[dict[str, Any]] = []
    for m in rows:
        if not is_monitor_panel_entry(m):
            continue
        if await can_view_monitor(m, user_id):
            visible.append(serialize_monitor(m, can_manage=_can_manage_monitor(m, user_id, tier)))
    return visible


async def list_company_members_for_user(user_id: str) -> list[dict[str, Any]]:
    tier = await get_user_tier(user_id)
    if tier not in ("admin", "internal"):
        raise HTTPException(status_code=403, detail="仅公司成员可查看成员列表")
    members: list[CompanyMember] = await monitor_db.list_company_members()
    return [
        {
            "user_id": m.user_id,
            "email": m.email,
            "name": m.name,
            "tier": m.tier,
        }
        for m in members
    ]


async def get_monitor_detail(slug: str, *, viewer_id: str) -> dict[str, Any]:
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)
    superseded = await _get_superseded_shared_document(monitor)
    if superseded is not None:
        if str(viewer_id or "").strip() != superseded.owner_user_id:
            raise HTTPException(
                status_code=403,
                detail="该文档已由分享文档接管，无权通过旧监控入口查看",
            )
        return await _build_monitor_detail_payload(
            monitor, viewer_id=viewer_id, read_only=True
        )
    await require_monitor_view(monitor, viewer_id)
    return await _build_monitor_detail_payload(monitor, viewer_id=viewer_id)


async def get_monitor_by_artifact(
    artifact_id: str, *, viewer_id: str
) -> dict[str, Any] | None:
    monitor = await monitor_db.get_monitor_by_artifact_id(artifact_id)
    if monitor is None or not is_monitor_capability_enabled(monitor):
        return None
    if await _get_superseded_shared_document(monitor) is not None:
        return None
    if str(viewer_id or "").strip() == monitor.owner_user_id:
        return serialize_monitor(monitor, can_manage=True)
    await require_monitor_view(monitor, viewer_id)
    tier = await get_user_tier(viewer_id)
    return serialize_monitor(monitor, can_manage=_can_manage_monitor(monitor, viewer_id, tier))


async def mint_grant_for_monitor(slug: str, *, viewer_id: str) -> dict[str, Any]:
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)
    superseded = await _get_superseded_shared_document(monitor)
    if superseded is not None:
        if str(viewer_id or "").strip() != superseded.owner_user_id:
            raise HTTPException(
                status_code=403,
                detail="该文档已由分享文档接管，无权领取旧监控读取授权",
            )
    else:
        await require_monitor_view(monitor, viewer_id)
    grant = await mint_monitor_grant(
        monitor.monitor_id, viewer_id, monitor.acl_revision
    )
    return {
        "monitor_grant": grant.token,
        "frame_base": frame_prefix(grant),
        "data_base": data_prefix(grant),
        "expires_in": seconds_left(grant.expires_at),
        "acl_revision": monitor.acl_revision,
    }


async def publish_workspace_monitor(
    payload: PublishMonitorRequest,
    *,
    caller_id: str,
    allow_clear_pause: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
) -> dict[str, Any]:
    members = await validate_publish_permissions(
        caller_id=caller_id,
        visibility=payload.visibility,
        member_user_ids=payload.member_user_ids,
    )
    try:
        workspace = await owned_workspace_on_computer(payload.workspace_id, caller_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(status_code=404, detail="Workspace not found") from None
        raise
    if workspace.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="Workspace not found")

    rel_path = _normalize_workspace_entry_path(workspace, payload.path)

    artifact = await artifacts_db.ensure_artifact(
        str(workspace["workspace_id"]), rel_path
    )
    existing = await monitor_db.get_monitor_by_artifact_id(artifact.artifact_id)
    is_downgrade_only = (
        existing is not None
        and existing.visibility == "public"
        and payload.visibility != "public"
    )
    if not is_artifact_publishing_enabled():
        if not is_downgrade_only:
            _require_publishing_enabled()
        assert existing is not None
        tier = await get_user_tier(caller_id)
        if not _can_manage_monitor(existing, caller_id, tier):
            raise_external_member_forbidden("无权修改此监控面板")
        if tier == "external" and existing.visibility != "own":
            raise_external_member_forbidden(EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE)
        updated_downgraded = await monitor_db.update_monitor_settings_tx(
            existing.monitor_id,
            artifact_id=existing.artifact_id,
            owner_user_id=caller_id,
            caller_id=caller_id,
            title=(payload.title or "").strip() or None,
            category=payload.category,
            sort_order=payload.sort_order,
            visibility=payload.visibility,
            keep_updated=payload.keep_updated,
            member_user_ids=members if payload.visibility == "members" else [],
            resolve_caller_tier=get_user_tier,
            reject_external_non_own_existing=True,
        )
        detail = await _build_monitor_detail_payload(updated_downgraded, viewer_id=caller_id)
        detail["cut"] = False
        return detail

    if existing is not None:
        tier = await get_user_tier(caller_id)
        if not _can_manage_monitor(existing, caller_id, tier):
            raise_external_member_forbidden("无权修改此监控面板")
        if tier == "external" and existing.visibility != "own":
            raise_external_member_forbidden(EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE)
        slug = existing.slug
    else:
        if payload.slug is not None:
            candidate = payload.slug.strip().lower()
            if not is_valid_slug(candidate):
                raise HTTPException(status_code=422, detail="固定地址 slug 格式无效或为保留名称")
            if await monitor_db.get_monitor_by_slug(candidate) is not None:
                raise HTTPException(status_code=409, detail="该固定地址已被占用")
            slug = candidate
        else:
            slug = await monitor_db.pick_available_slug(
                payload.title or rel_path.rsplit("/", 1)[-1].removesuffix(".html")
            )

    title = (payload.title or "").strip() or (
        existing.title if existing else rel_path.rsplit("/", 1)[-1]
    )

    try:
        snap = await artifact_snapshot.capture_workspace_monitor_snapshot(
            artifact,
            user_id=caller_id,
            mode=ReadMode.OWNER,
            reviewed=payload.files,
            loaded=tuple(payload.loaded),
            earlier=tuple(payload.earlier),
            reviewed_page=payload.page_sha256,
        )
    except Exception as exc:
        if existing is not None:
            await monitor_db.record_monitor_publish_failure(
                existing.monitor_id,
                error_message=str(exc),
            )
        raise _translate_snapshot_error(exc) from None

    effective_title = (payload.title or "").strip() or snap.title or title
    try:
        updated_monitor, result = await monitor_db.commit_monitor_publish_tx(
            slug=slug,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=caller_id,
            monitor_owner_id=caller_id,
            title=effective_title,
            category=payload.category,
            sort_order=payload.sort_order,
            visibility=payload.visibility,
            keep_updated=payload.keep_updated,
            source_kind="workspace",
            source_ref=f"{workspace['workspace_id']}:{rel_path}",
            member_user_ids=members,
            files=snap.files,
            label=payload.label,
            read_basis=ReadBasis.of(artifact),
            origin="owner",
            loaded_files=[e.path for e in snap.entries if e.reason == REASON_LOADED],
            left_out_files=[e.path for e in snap.left_out],
            verify_public_authority=_verify_user_is_admin,
            reject_if_pending_outbound_confirm=reject_if_pending_outbound_confirm,
        )
    except Exception as exc:
        from src.dalton.colleague_outbound_guard import WorkspaceAutoPublishPaused

        if existing is not None and not isinstance(exc, WorkspaceAutoPublishPaused):
            await monitor_db.record_monitor_publish_failure(
                existing.monitor_id,
                error_message=_extract_publish_failure_message(exc),
            )
        raise _translate_snapshot_error(exc) from None

    detail = await _build_monitor_detail_payload(updated_monitor, viewer_id=caller_id)
    detail["cut"] = result.cut
    # Only explicit web manual publish by the workspace owner may CAS-clear pending_outbound_confirm
    if allow_clear_pause and str(caller_id) == str(workspace.get("user_id") or ""):
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled():
            from src.dalton.colleague_outbound_guard import clear_pending_outbound_confirm

            clear_res = await clear_pending_outbound_confirm(
                str(workspace["workspace_id"]),
                expected_pause_token=payload.confirm_pause_token,
            )
            if not clear_res.cleared and clear_res.status != "not_paused":
                detail["auto_publish_paused"] = True
                detail["auto_publish_pause_token"] = clear_res.current_pause_token
                detail["auto_publish_pause_notice"] = clear_res.notice
    return detail


async def update_monitor_settings(
    slug: str, payload: UpdateMonitorRequest, *, caller_id: str
) -> dict[str, Any]:
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)
    if await _get_superseded_shared_document(monitor) is not None:
        raise HTTPException(
            status_code=403,
            detail="该文档已由分享文档接管，旧监控入口仅保留历史只读记录",
        )

    tier = await get_user_tier(caller_id)
    if not _can_manage_monitor(monitor, caller_id, tier):
        raise_external_member_forbidden("只有面板所有者或管理员可修改设置")

    validated_members: list[str] | None = None
    if payload.visibility is not None or payload.member_user_ids is not None:
        if payload.visibility is not None:
            # Validate caller tier against explicit visibility change; if member_user_ids
            # was omitted when keeping/setting 'members', check existing/locked members inside tx.
            if tier == "external" and payload.visibility != "own":
                raise_external_member_forbidden(
                    "外部成员发布监控面板时只能选择“仅自己”可见"
                )
            if payload.visibility == "public" and tier != "admin":
                raise_admin_required("只有管理员可以将监控面板设为公开链接")
            if payload.visibility == "public" and monitor.visibility != "public":
                _require_publishing_enabled()
        if payload.member_user_ids is not None:
            if tier == "external" and payload.member_user_ids:
                raise_external_member_forbidden(
                    "外部成员发布监控面板时只能选择“仅自己”可见"
                )
            if payload.visibility == "members" or (
                payload.visibility is None and payload.member_user_ids
            ):
                validated_members = await validate_publish_permissions(
                    caller_id=caller_id,
                    visibility="members",
                    member_user_ids=payload.member_user_ids,
                )
            else:
                validated_members = []

    next_title = (payload.title or "").strip() or None if payload.title is not None else None
    workspace_owner_id = await _resolve_workspace_owner_id(monitor)

    async def _verify_data_job_patch_approver(_owner_id: str, *, conn: Any = None) -> None:
        binding = get_registered_monitor_binding_by_slug(
            monitor.source_ref or "", monitor.slug
        )
        if binding is None:
            raise PublicAuthorityLostError("公开数据任务监控必须在登记表中指定审批管理员")
        await _verify_data_job_public_approver(
            binding,
            is_existing_public=(monitor.visibility == "public"),
            conn=conn,
        )

    verify_fn = (
        _verify_data_job_patch_approver
        if monitor.source_kind == "data_job"
        else _verify_user_is_admin
    )

    patch_binding = (
        get_registered_monitor_binding_by_slug(monitor.source_ref or "", monitor.slug)
        if monitor.source_kind == "data_job"
        else None
    )
    patch_approver_id = (
        (patch_binding.approved_by_admin_user_id or "").strip() or None
        if patch_binding is not None
        else None
    )

    try:
        updated = await monitor_db.update_monitor_settings_tx(
            monitor.monitor_id,
            artifact_id=monitor.artifact_id,
            owner_user_id=workspace_owner_id,
            caller_id=caller_id,
            title=next_title,
            category=payload.category,
            sort_order=payload.sort_order,
            visibility=payload.visibility,
            keep_updated=payload.keep_updated,
            member_user_ids=validated_members,
            require_publishing_enabled_fn=_require_publishing_enabled,
            verify_public_authority=verify_fn,
            resolve_caller_tier=get_user_tier,
            public_approver_id=patch_approver_id,
        )
    except PublicAuthorityLostError as exc:
        raise_admin_required(str(exc))

    return await _build_monitor_detail_payload(updated, viewer_id=caller_id)


async def republish_workspace_monitor(
    slug: str,
    payload: RepublishMonitorRequest,
    *,
    caller_id: str,
    allow_clear_pause: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
) -> dict[str, Any]:
    _require_publishing_enabled()
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)
    if await _get_superseded_shared_document(monitor) is not None:
        raise HTTPException(
            status_code=403,
            detail="该文档已由分享文档接管，旧监控入口仅保留历史只读记录",
        )
    tier = await get_user_tier(caller_id)
    if not _can_manage_monitor(monitor, caller_id, tier):
        raise_external_member_forbidden("无权重新发布此监控面板")
    if monitor.source_kind != "workspace":
        raise HTTPException(status_code=400, detail="数据任务面板请由数据任务触发更新")
    if tier == "external" and monitor.visibility != "own":
        try:
            require_monitor_publish_visibility(tier, monitor.visibility)
        except HTTPException as exc:
            await monitor_db.record_monitor_publish_failure(
                monitor.monitor_id,
                error_message=_extract_publish_failure_message(exc),
            )
            raise
    if monitor.visibility == "public" and tier != "admin":
        await monitor_db.record_monitor_publish_failure(
            monitor.monitor_id, error_message=PUBLIC_PERMISSION_LOST_MESSAGE
        )
        raise_admin_required(PUBLIC_PERMISSION_LOST_MESSAGE)
    try:
        require_monitor_publish_visibility(tier, monitor.visibility)
    except HTTPException as exc:
        await monitor_db.record_monitor_publish_failure(
            monitor.monitor_id,
            error_message=_extract_publish_failure_message(exc),
        )
        raise

    workspace_owner_id = await _resolve_workspace_owner_id(monitor)
    artifact = await artifacts_db.get_artifact(
        monitor.artifact_id, user_id=workspace_owner_id
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="关联文件不存在")

    takeover_owner = (
        caller_id
        if monitor.visibility == "public" and tier == "admin" and caller_id != monitor.owner_user_id
        else None
    )

    try:
        snap = await artifact_snapshot.capture_workspace_monitor_snapshot(
            artifact,
            user_id=workspace_owner_id,
            mode=ReadMode.OWNER,
            reviewed=payload.files,
            loaded=tuple(payload.loaded),
            earlier=tuple(payload.earlier),
            reviewed_page=payload.page_sha256,
        )
        updated, result, _deduped = await cut_monitor_version(
            monitor=monitor,
            owner_user_id=workspace_owner_id,
            files=snap.files,
            title=monitor.title,
            label=payload.label,
            read_basis=ReadBasis.of(artifact),
            origin="owner",
            loaded_files=[e.path for e in snap.entries if e.reason == REASON_LOADED],
            left_out_files=[e.path for e in snap.left_out],
            new_owner_user_id=takeover_owner,
            caller_user_id=caller_id,
            verify_public_authority=_verify_user_is_admin,
            reject_if_pending_outbound_confirm=reject_if_pending_outbound_confirm,
        )
    except Exception as exc:
        from src.dalton.colleague_outbound_guard import WorkspaceAutoPublishPaused

        is_superseded = (
            isinstance(exc, HTTPException)
            and exc.status_code == 403
            and "分享文档接管" in str(exc.detail or "")
        )
        if not isinstance(exc, WorkspaceAutoPublishPaused) and not is_superseded:
            await monitor_db.record_monitor_publish_failure(
                monitor.monitor_id,
                error_message=_extract_publish_failure_message(exc),
            )
        raise _translate_snapshot_error(exc) from None

    detail = await _build_monitor_detail_payload(updated, viewer_id=caller_id)
    detail["cut"] = bool(result.cut) if result is not None else False
    # Only explicit web manual republish by the workspace owner may CAS-clear pending_outbound_confirm
    if allow_clear_pause and artifact.workspace_id and str(caller_id) == str(workspace_owner_id):
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled():
            from src.dalton.colleague_outbound_guard import clear_pending_outbound_confirm

            clear_res = await clear_pending_outbound_confirm(
                str(artifact.workspace_id),
                expected_pause_token=payload.confirm_pause_token,
            )
            if not clear_res.cleared and clear_res.status != "not_paused":
                detail["auto_publish_paused"] = True
                detail["auto_publish_pause_token"] = clear_res.current_pause_token
                detail["auto_publish_pause_notice"] = clear_res.notice
    return detail


async def delete_monitor_by_slug(slug: str, *, caller_id: str) -> dict[str, Any]:
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)
    if await _get_superseded_shared_document(monitor) is not None:
        raise HTTPException(
            status_code=403,
            detail="该文档已由分享文档接管，旧监控入口仅保留历史只读记录",
        )
    tier = await get_user_tier(caller_id)
    if not _can_manage_monitor(monitor, caller_id, tier):
        raise HTTPException(status_code=403, detail="无权删除此监控面板")
    workspace_owner_id = await _resolve_workspace_owner_id(monitor)
    await monitor_db.delete_monitor_tx(
        monitor.monitor_id,
        artifact_id=monitor.artifact_id,
        workspace_owner_id=workspace_owner_id,
        caller_id=caller_id,
        resolve_caller_tier=get_user_tier,
    )
    return {"deleted": True, "slug": slug}


async def republish_workspace_monitors_after_turn(*, workspace_id: str) -> None:
    """Refresh every workspace monitor with ``keep_updated=TRUE`` after a turn completes.

    Runs independently of ``share_links.shared_at`` and ``kept_at``. Preserves the
    previous ``published_seq`` and records ``last_publish_error`` if a cut fails,
    dependency files are absent, or a public monitor's publisher lost admin rights.
    """
    if not is_artifact_publishing_enabled():
        return
    monitors = await monitor_db.list_workspace_auto_update_monitors(workspace_id)
    if not monitors:
        return
    workspace = await db_get_workspace(workspace_id)
    if not workspace or workspace.get("status") != "running":
        return
    workspace_owner_id = str(workspace["user_id"])
    from src.dalton.settings import is_dalton_sharing_auth_enabled

    sharing_auth_enabled = is_dalton_sharing_auth_enabled()
    if sharing_auth_enabled:
        from src.dalton.colleague_outbound_guard import (
            is_workspace_pending_outbound_confirm,
        )

        if is_workspace_pending_outbound_confirm(workspace):
            logger.info(
                "Skipping auto-update for workspace %s monitors: pending_outbound_confirm",
                workspace_id,
            )
            return

    for monitor in monitors:
        if await _get_superseded_shared_document(monitor) is not None:
            continue
        if sharing_auth_enabled:
            from src.dalton.colleague_outbound_guard import (
                is_workspace_pending_outbound_confirm,
            )

            try:
                fresh_ws = await db_get_workspace(workspace_id)
            except Exception:
                logger.warning(
                    "Skipping auto-update for workspace monitor %s (%s): failed to re-read workspace %s",
                    monitor.slug,
                    monitor.monitor_id,
                    workspace_id,
                    exc_info=True,
                )
                continue
            if not fresh_ws or is_workspace_pending_outbound_confirm(fresh_ws):
                logger.info(
                    "Skipping auto-update for workspace monitor %s (%s): pending_outbound_confirm",
                    monitor.slug,
                    monitor.monitor_id,
                )
                continue
        try:
            owner_tier = await get_user_tier(monitor.owner_user_id)
            if owner_tier == "external" and monitor.visibility != "own":
                require_monitor_publish_visibility(owner_tier, monitor.visibility)
            if monitor.visibility == "public" and owner_tier != "admin":
                raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
            require_monitor_publish_visibility(owner_tier, monitor.visibility)
            artifact = await artifacts_db.get_artifact(
                monitor.artifact_id, user_id=workspace_owner_id
            )
            if artifact is None:
                continue
            snap = await artifact_snapshot.capture_workspace_monitor_snapshot(
                artifact,
                user_id=workspace_owner_id,
                mode=ReadMode.WARM_ONLY,
            )
            await _abort_if_absent_dependencies(
                monitor.artifact_id,
                monitor.published_seq or artifact.latest_seq,
                snap,
            )
            await cut_monitor_version(
                monitor=monitor,
                owner_user_id=workspace_owner_id,
                files=snap.files,
                title=monitor.title,
                read_basis=ReadBasis.of(artifact),
                origin="owner",
                loaded_files=[
                    e.path for e in snap.entries if e.reason == REASON_LOADED
                ],
                artifact=artifact,
                verify_public_authority=_verify_user_is_admin,
                check_pending_outbound_confirm=sharing_auth_enabled,
            )
        except Exception as exc:
            logger.warning(
                "Auto-updating workspace monitor %s (%s) failed: %s",
                monitor.slug,
                monitor.monitor_id,
                exc,
                exc_info=True,
            )
            await monitor_db.record_monitor_publish_failure(
                monitor.monitor_id,
                error_message=_extract_publish_failure_message(exc),
            )


async def _verify_data_job_public_approver(
    binding: MonitorDataJobBinding,
    *,
    is_existing_public: bool = False,
    conn: Any = None,
) -> None:
    approver = (binding.approved_by_admin_user_id or "").strip()
    if not approver:
        detail = (
            PUBLIC_PERMISSION_LOST_MESSAGE
            if is_existing_public
            else "公开数据任务监控必须在登记表中指定审批管理员"
        )
        raise PublicAuthorityLostError(detail)
    from src.dalton.settings import is_dalton_local_auth_enabled

    if (
        is_dalton_local_auth_enabled()
        and conn is not None
        and get_user_tier is _REAL_GET_USER_TIER
        and hasattr(conn, "cursor")
    ):
        async with conn.cursor(row_factory=dict_row) as cur:
            approver_tier = await monitor_db._resolve_live_tier_in_cur(cur, approver)
    else:
        approver_tier = await monitor_db._call_with_optional_conn(
            get_user_tier, approver, conn=conn
        )
    if approver_tier != "admin":
        raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)


def _binding_entry_path(binding: MonitorDataJobBinding, *, is_multi: bool = False) -> str:
    """Return the workspace-unique artifact path for ``binding`` (always ``<slug>/<entry_file>``)."""
    _ = is_multi
    return f"{binding.slug}/{binding.entry_file}"


def _open_binding_reader_source(
    data_root: Path, binding: MonitorDataJobBinding, *, is_multi: bool = False
) -> tuple[Any, str, tuple[str, ...]]:
    """Open contained FileSource(s) for ``binding`` under ``<slug>/`` (plus ``_shared/`` when configured)."""
    _ = is_multi
    panel_source = HostDirFileSource.open_registered(data_root, binding)
    mounts: dict[str, HostDirFileSource] = {binding.slug: panel_source}
    try:
        if binding.shared_rel_dir:
            shared_path = data_root / binding.shared_rel_dir
            if shared_path.exists() or shared_path.is_symlink():
                mounts["_shared"] = HostDirFileSource.open_registered_dir(
                    data_root, binding.shared_rel_dir
                )
    except Exception:
        panel_source.close()
        raise
    scoped = ScopedMountFileSource(mounts, primary_mount=binding.slug)
    work_dir = str(data_root / Path(binding.rel_dir).parent)
    extra_files = () if binding.dependencies_only else scoped.list_primary_artifact_files()
    return scoped, work_dir, extra_files


def _is_migratable_legacy_data_job_monitor(
    monitor: MonitorRecord,
    binding: MonitorDataJobBinding,
    *,
    is_multi: bool = False,
) -> bool:
    """Return True if ``monitor`` is a legacy shared-``index.html`` single-binding data-job row for ``binding``."""
    if is_multi:
        return False
    expected_entry = _binding_entry_path(binding, is_multi=is_multi)
    return (
        monitor.source_kind == "data_job"
        and is_company_system_user(monitor.owner_user_id)
        and monitor.slug == binding.slug
        and monitor.entry_path == binding.entry_file
        and monitor.entry_path != expected_entry
    )


async def _republish_single_binding(
    binding: MonitorDataJobBinding,
    *,
    clean_run_id: str,
    data_root: Path,
    existing: MonitorRecord | None,
    is_multi: bool,
) -> dict[str, Any]:
    if existing is not None and await monitor_db.has_processed_monitor_run(
        existing.monitor_id, clean_run_id
    ):
        return {
            "monitor_id": existing.monitor_id,
            "slug": existing.slug,
            "published_seq": existing.published_seq,
            "cut": False,
            "deduplicated": True,
        }

    if binding.visibility == "public" and not (binding.approved_by_admin_user_id or "").strip():
        if existing is not None:
            await monitor_db.record_monitor_publish_failure(
                existing.monitor_id,
                error_message=PUBLIC_PERMISSION_LOST_MESSAGE,
            )
        raise HTTPException(
            status_code=403,
            detail="公开数据任务监控必须在登记表中指定审批管理员",
        )

    is_public_target = binding.visibility == "public" or (
        existing is not None and existing.visibility == "public"
    )
    if is_public_target:
        try:
            await _verify_data_job_public_approver(
                binding,
                is_existing_public=(existing is not None and existing.visibility == "public"),
            )
        except PublicAuthorityLostError as exc:
            if existing is not None:
                await monitor_db.record_monitor_publish_failure(
                    existing.monitor_id, error_message=str(exc)
                )
            raise HTTPException(status_code=403, detail=str(exc)) from None

    entry_path = _binding_entry_path(binding, is_multi=is_multi)
    if existing is None:
        if not is_valid_slug(binding.slug):
            raise HTTPException(
                status_code=500,
                detail=f"数据任务登记表中的 slug '{binding.slug}' 不合法",
            )
        conflict = await monitor_db.get_monitor_by_slug(binding.slug)
        if conflict is not None:
            if _is_migratable_legacy_data_job_monitor(conflict, binding, is_multi=is_multi):
                existing = conflict
                if await monitor_db.has_processed_monitor_run(
                    existing.monitor_id, clean_run_id
                ):
                    return {
                        "monitor_id": existing.monitor_id,
                        "slug": existing.slug,
                        "published_seq": existing.published_seq,
                        "cut": False,
                        "deduplicated": True,
                    }
                if existing.visibility == "public":
                    try:
                        await _verify_data_job_public_approver(
                            binding,
                            is_existing_public=True,
                        )
                    except PublicAuthorityLostError as exc:
                        await monitor_db.record_monitor_publish_failure(
                            existing.monitor_id, error_message=str(exc)
                        )
                        raise HTTPException(status_code=403, detail=str(exc)) from None
            else:
                logger.error(
                    "Data-job monitor slug collision: job_key=%s slug=%s occupied_by=%s",
                    binding.job_key,
                    binding.slug,
                    conflict.monitor_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"固定 slug '{binding.slug}' 已被已有监控面板占用"
                        f"（monitor_id={conflict.monitor_id}），禁止自动改名"
                    ),
                )

    async def _verify_job_public_in_tx(_owner_id: str, *, conn: Any = None) -> None:
        await _verify_data_job_public_approver(
            binding, is_existing_public=True, conn=conn
        )

    monitor = existing
    try:
        source, work_dir, extra_files = _open_binding_reader_source(
            data_root, binding, is_multi=is_multi
        )
        with source:
            reader = Reader(
                files=source,
                work_dir=work_dir,
                previous_dirs=(),
            )
            snap = await artifact_snapshot.capture_reader_monitor_snapshot(
                reader,
                owner_id=COMPANY_SYSTEM_USER_ID,
                entry_path=entry_path,
                extra_paths=extra_files,
            )
        workspace_id = await monitor_db.ensure_company_monitor_workspace()
        if monitor is not None and _is_migratable_legacy_data_job_monitor(
            monitor, binding, is_multi=is_multi
        ):
            logger.info(
                "Auto-migrating legacy data-job monitor: job_key=%s slug=%s monitor_id=%s from=%s to=%s",
                binding.job_key,
                binding.slug,
                monitor.monitor_id,
                monitor.entry_path,
                entry_path,
            )
            updated, result, deduped = await monitor_db.migrate_data_job_monitor_and_cut_tx(
                monitor_id=monitor.monitor_id,
                artifact_id=monitor.artifact_id,
                workspace_id=workspace_id,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                target_entry_path=entry_path,
                legacy_entry_path=binding.entry_file,
                expected_slug=binding.slug,
                target_source_ref=binding.job_key,
                files=snap.files,
                title=monitor.title,
                origin="owner",
                run_id=clean_run_id,
                verify_public_authority=_verify_job_public_in_tx,
            )
        else:
            artifact = await artifacts_db.ensure_artifact(workspace_id, entry_path)
            if monitor is not None and monitor.artifact_id != artifact.artifact_id:
                logger.error(
                    "Data-job monitor artifact mismatch: job_key=%s slug=%s monitor_id=%s monitor_artifact=%s expected_entry=%s expected_artifact=%s",
                    binding.job_key,
                    binding.slug,
                    monitor.monitor_id,
                    monitor.artifact_id,
                    entry_path,
                    artifact.artifact_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"监控面板 '{binding.slug}' 仍绑定旧资源路径 '{monitor.entry_path}'，"
                        f"无法自动迁移到 '{entry_path}'"
                    ),
                )
            if monitor is None:
                monitor = await monitor_db.upsert_monitor(
                    slug=binding.slug,
                    artifact_id=artifact.artifact_id,
                    owner_user_id=COMPANY_SYSTEM_USER_ID,
                    title=binding.title,
                    category=binding.category,
                    sort_order=binding.sort_order,
                    visibility=binding.visibility,
                    keep_updated=True,
                    source_kind="data_job",
                    source_ref=binding.job_key,
                    member_user_ids=(),
                )
            await _abort_if_absent_dependencies(
                monitor.artifact_id,
                monitor.published_seq or artifact.latest_seq,
                snap,
            )
            updated, result, deduped = await cut_monitor_version(
                monitor=monitor,
                owner_user_id=COMPANY_SYSTEM_USER_ID,
                files=snap.files,
                title=monitor.title,
                read_basis=ReadBasis.of(artifact),
                origin="owner",
                run_id=clean_run_id,
                artifact=artifact,
                verify_public_authority=_verify_job_public_in_tx,
            )
    except Exception as exc:
        if monitor is not None:
            await monitor_db.record_monitor_publish_failure(
                monitor.monitor_id,
                error_message=str(exc),
            )
        raise _translate_snapshot_error(exc) from None

    return {
        "monitor_id": updated.monitor_id,
        "slug": updated.slug,
        "published_seq": updated.published_seq,
        "cut": bool(result.cut) if result is not None else False,
        "deduplicated": deduped,
    }


async def republish_data_job_monitor(
    *,
    job_key: str,
    run_id: str,
    data_root: Path | None = None,
) -> dict[str, Any]:
    """Republish registered data-job monitor(s) from their pre-registered host output dir."""
    if not has_capability(CapabilityId.DATA_JOBS):
        raise HTTPException(status_code=404, detail="数据任务监控未启用")
    _require_publishing_enabled()
    bindings = get_registered_monitor_bindings(job_key)
    if not bindings:
        raise HTTPException(
            status_code=403,
            detail=f"未登记的数据任务监控映射: {job_key}",
        )

    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        raise HTTPException(status_code=400, detail="run_id is required")

    if data_root is None:
        from src.dalton.settings import get_dalton_backend_var_dir

        data_root = Path(get_dalton_backend_var_dir()) / "openclaw-data"

    if len(bindings) == 1:
        binding = bindings[0]
        existing = await monitor_db.get_monitor_by_data_job(binding.job_key)
        if existing is not None and existing.slug != binding.slug:
            logger.error(
                "Data-job monitor slug/source_ref mismatch: job_key=%s expected_slug=%s actual_slug=%s monitor_id=%s",
                binding.job_key,
                binding.slug,
                existing.slug,
                existing.monitor_id,
            )
            existing = None
        return await _republish_single_binding(
            binding,
            clean_run_id=clean_run_id,
            data_root=data_root,
            existing=existing,
            is_multi=False,
        )

    existing_rows = await monitor_db.list_monitors_by_data_job(job_key)
    by_entry = {row.entry_path: row for row in existing_rows if row.entry_path}
    by_slug = {row.slug: row for row in existing_rows}

    results: list[dict[str, Any]] = []
    first_exc: Exception | None = None
    for binding in bindings:
        entry_path = _binding_entry_path(binding, is_multi=True)
        existing = by_entry.get(entry_path) or by_slug.get(binding.slug)
        try:
            res = await _republish_single_binding(
                binding,
                clean_run_id=clean_run_id,
                data_root=data_root,
                existing=existing,
                is_multi=True,
            )
            results.append(res)
        except Exception as exc:
            if first_exc is None:
                first_exc = exc
            logger.warning(
                "Republishing data-job monitor %s (%s) failed: %s",
                binding.slug,
                binding.job_key,
                exc,
                exc_info=True,
            )

    if first_exc is not None:
        raise first_exc

    return {
        "job_key": job_key,
        "monitors": results,
        "cut": any(r["cut"] for r in results),
        "deduplicated": all(r["deduplicated"] for r in results),
    }


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def _enrich_monitor_publish_failure_message(
    *,
    job_key: str,
    terminal_status: str,
    failure_reason: str | None,
    data_root: Path | None,
) -> str:
    """Enrich bare failure codes (e.g. ``exit_code_1``) with monitor state details
    such as per-index actual data dates and missing-constituent failure reasons."""
    raw = str(failure_reason or "").strip()
    is_bare_exit = not raw or raw.startswith("exit_code_")
    if job_key != "hk-momentum" or data_root is None:
        return raw or terminal_status

    st_dir = Path(data_root) / "state" / "monitor-feeds" / "hk_momentum"
    meta = _read_json_if_exists(st_dir / "index_meta.json") or {}
    status_doc = _read_json_if_exists(st_dir / "status.json") or {}

    index_dates_raw = meta.get("index_dates")
    date_parts: list[str] = []
    if isinstance(index_dates_raw, dict):
        for k, v in index_dates_raw.items():
            if v is not None and str(v).strip():
                date_parts.append(f"{k}={v}")
    elif meta.get("last_date"):
        date_parts.append(f"last_date={meta['last_date']}")

    detail_parts: list[str] = []
    if not is_bare_exit:
        detail_parts.append(raw)
    else:
        meta_summary = str(meta.get("failure_summary") or "").strip()
        if meta_summary:
            return meta_summary
        stale_reason = str(meta.get("stale_reason") or "").strip()
        if stale_reason:
            detail_parts.append(stale_reason)
        overall = status_doc.get("overall") if isinstance(status_doc.get("overall"), dict) else {}
        overall_reason = str(overall.get("reason") or "").strip()
        if overall_reason and overall_reason not in detail_parts:
            detail_parts.append(overall_reason)
        missing_by_idx = meta.get("missing_held_by_index")
        if isinstance(missing_by_idx, dict) and not detail_parts:
            for idx_key, codes in missing_by_idx.items():
                if isinstance(codes, list) and codes:
                    preview = ", ".join(str(c) for c in codes[:5])
                    detail_parts.append(
                        f"{idx_key} 持有成分缺少必要价格 {len(codes)} 只 (如 {preview})"
                    )

    headline = "；".join(detail_parts) if detail_parts else (raw or terminal_status)
    if date_parts and "数据日期:" not in headline:
        return f"{headline} | 数据日期: {', '.join(date_parts)}"
    return headline


async def maybe_publish_data_job_monitor(
    *,
    job_key: str,
    run_id: str,
    terminal_status: str,
    failure_reason: str | None = None,
    data_root: Path | None = None,
) -> None:
    """Scheduler post-run hook: republish on success, record failure without touching ``published_seq``."""
    if not has_capability(CapabilityId.DATA_JOBS):
        return
    bindings = get_registered_monitor_bindings(job_key)
    if not bindings:
        return
    if not is_artifact_publishing_enabled():
        return
    if terminal_status == "succeeded":
        await republish_data_job_monitor(
            job_key=job_key, run_id=run_id, data_root=data_root
        )
        return
    if len(bindings) == 1:
        existing = await monitor_db.get_monitor_by_data_job(bindings[0].job_key)
        existing_rows = (
            [existing]
            if existing is not None and existing.slug == bindings[0].slug
            else []
        )
    else:
        existing_rows = [
            row
            for row in await monitor_db.list_monitors_by_data_job(job_key)
            if any(row.slug == b.slug for b in bindings)
        ]
    enriched_error = _enrich_monitor_publish_failure_message(
        job_key=job_key,
        terminal_status=terminal_status,
        failure_reason=failure_reason,
        data_root=data_root,
    )
    for existing in existing_rows:
        await monitor_db.record_monitor_publish_failure(
            existing.monitor_id,
            error_message=enriched_error,
        )


async def _authorize_grant(grant_token: str) -> tuple[MonitorRecord, str]:
    from src.server.services import account as accounts

    try:
        verified = await verify_monitor_grant(grant_token)
    except MonitorGrantError:
        raise HTTPException(status_code=403, detail="监控面板授权无效或已过期") from None

    if (
        await accounts.read_standing(verified.user_id, unknown_on_cache_error=True)
    ).suspended:
        raise HTTPException(status_code=403, detail="监控面板授权无效或已过期")

    monitor = await monitor_db.get_monitor_by_id(verified.monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    require_monitor_capability(monitor)

    # 应用内 grant（/monitors/g/{grant}/*）对停用所有者保守拒绝。匿名公开入口
    # （/monitors/public/{slug}*）只看 visibility == "public"，不随所有者停用下线；
    # Dalton 以 OSS 模式运行（teams 关闭），read_standing 不会返回停用。
    if (
        not is_company_system_user(monitor.owner_user_id)
        and (
            await accounts.read_standing(
                monitor.owner_user_id, unknown_on_cache_error=True
            )
        ).suspended
    ):
        raise HTTPException(status_code=403, detail="监控面板授权无效或已过期")

    if monitor.acl_revision != verified.acl_revision:
        raise HTTPException(status_code=403, detail="监控面板权限已变更，授权已失效")

    superseded = await _get_superseded_shared_document(monitor)
    if superseded is not None:
        if str(verified.user_id or "").strip() != superseded.owner_user_id:
            raise HTTPException(
                status_code=403,
                detail="该文档已由分享文档接管，旧监控授权已失效",
            )
        return monitor, verified.user_id

    await require_monitor_view(monitor, verified.user_id)
    return monitor, verified.user_id


async def _serve_monitor_version_file(
    monitor: MonitorRecord,
    seq: int,
    path: str,
    *,
    if_none_match: str | None = None,
    head: bool = False,
) -> ServedArtifactFile:
    if not await monitor_db.is_monitor_version_published(monitor.monitor_id, seq):
        raise HTTPException(status_code=404, detail="File not found")

    stored = await artifacts_db.get_version_file(monitor.artifact_id, seq, path)
    if stored is None:
        raise HTTPException(status_code=404, detail="File not found")

    content_type = _content_type(stored.content_type)
    bridged = _is_html(content_type) and _charset_is_utf8(content_type)
    bridge = bridge_for("view")
    etag = f"{stored.sha256}-{bridge.digest}" if bridged else stored.sha256
    headers = _headers(
        content_type,
        etag,
        cache_control="private, no-cache",
        sandboxed=True,
        frame_ancestors=PREVIEW_PROXY_FRAME_ANCESTORS,
    )
    if if_none_match is not None and _etag_matches(if_none_match, etag):
        return ServedArtifactFile(body=b"", headers=headers, status=304)
    if head:
        if not bridged:
            headers.append(("content-length", str(stored.byte_len)))
        return ServedArtifactFile(body=b"", headers=headers)

    if stored.content is not None:
        body = stored.content
    else:
        try:
            body = await fetch_blob(stored.user_id, stored.sha256)
        except Exception as exc:
            raise ArtifactFileUnavailable(str(exc)) from exc

    if bridged:
        body = await asyncio.to_thread(with_bridge, body, bridge)
    headers.append(("content-length", str(len(body))))
    return ServedArtifactFile(body=body, headers=headers)


async def serve_monitor_file(
    grant_token: str,
    seq: str,
    path: str,
    *,
    if_none_match: str | None = None,
    head: bool = False,
) -> ServedArtifactFile:
    """Serve a published monitor version file under ``CSP: sandbox`` (opaque origin)."""
    if is_shared_document_grant(grant_token):
        from src.dalton.monitors import shared_documents

        return await shared_documents.serve_shared_document_file(
            grant_token,
            seq,
            path,
            if_none_match=if_none_match,
            head=head,
        )
    monitor, _viewer_id = await _authorize_grant(grant_token)

    if not seq.isdigit() or int(seq) < 1 or not _valid_path(path):
        raise HTTPException(status_code=404, detail="File not found")

    return await _serve_monitor_version_file(
        monitor,
        int(seq),
        path,
        if_none_match=if_none_match,
        head=head,
    )


def _resolve_public_entry_relative_path(entry_path: str, rel: str) -> str | None:
    """Resolve *rel* against *entry_path*'s directory strictly within the version root.

    Returns ``None`` if *rel* is empty, malformed, carries a URL scheme, or traverses
    above the workspace root. Never falls back to a same-named file at workspace root.
    """
    raw = str(rel or "").strip()
    if (
        not raw
        or len(raw) > 1024
        or "\\" in raw
        or "\x00" in raw
        or raw.startswith("//")
        or raw.startswith("__wsref__/")
    ):
        return None
    first_seg = raw.split("/", 1)[0]
    if ":" in first_seg:
        return None

    if raw.startswith("/"):
        stack: list[str] = []
    else:
        entry_dir = posixpath.dirname(entry_path or "")
        stack = [seg for seg in entry_dir.split("/") if seg]

    for seg in raw.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if not stack:
                return None
            stack.pop()
            continue
        stack.append(seg)

    if not stack:
        return None
    resolved = "/".join(stack)
    return resolved if _valid_path(resolved) else None


async def serve_public_monitor_file(
    slug: str,
    seq: str,
    path: str,
    *,
    rel: str | None = None,
    if_none_match: str | None = None,
    head: bool = False,
) -> ServedArtifactFile:
    """Anonymously serve the current published version file when ``visibility == 'public'``."""
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if (
        monitor is None
        or monitor.visibility != "public"
        or not is_monitor_capability_enabled(monitor)
        or await _get_superseded_shared_document(monitor) is not None
    ):
        raise HTTPException(status_code=404, detail="File not found")
    if (
        not seq.isdigit()
        or int(seq) < 1
        or monitor.published_seq <= 0
        or int(seq) != monitor.published_seq
    ):
        raise HTTPException(status_code=404, detail="File not found")

    if rel is not None:
        resolved = _resolve_public_entry_relative_path(monitor.entry_path or "", rel)
        if resolved is None:
            raise HTTPException(status_code=404, detail="File not found")
        return await _serve_monitor_version_file(
            monitor,
            int(seq),
            resolved,
            if_none_match=if_none_match,
            head=head,
        )

    target_path = path or (monitor.entry_path or "")
    if not target_path or not _valid_path(target_path):
        raise HTTPException(status_code=404, detail="File not found")
    entry_dir = posixpath.dirname(monitor.entry_path or "")
    prefer_nested = bool(
        path and entry_dir and not path.startswith(f"{entry_dir}/")
    )
    if prefer_nested:
        nested = posixpath.normpath(f"{entry_dir}/{path}")
        if nested.startswith(f"{entry_dir}/") and _valid_path(nested):
            try:
                return await _serve_monitor_version_file(
                    monitor,
                    int(seq),
                    nested,
                    if_none_match=if_none_match,
                    head=head,
                )
            except HTTPException as exc:
                if exc.status_code != 404 or not is_html_entry_path(monitor.entry_path):
                    raise
    return await _serve_monitor_version_file(
        monitor,
        int(seq),
        target_path,
        if_none_match=if_none_match,
        head=head,
    )


async def _read_monitor_data_response(
    monitor: MonitorRecord,
    data_key: str,
    *,
    if_none_match: str | None = None,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    entry = await monitor_db.get_monitor_data(monitor.monitor_id, data_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Monitor data key not found")

    response_body = {
        "monitor_id": monitor.monitor_id,
        "slug": monitor.slug,
        "key": entry.data_key,
        "as_of": entry.as_of.isoformat(),
        "sha256": entry.sha256,
        "updated_at": entry.updated_at.isoformat(),
        "data": entry.payload,
    }
    etag = hashlib.sha256(
        json.dumps(
            response_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    headers = {
        "ETag": f'"{etag}"',
        "Cache-Control": "private, no-cache",
    }
    if if_none_match is not None and _etag_matches(if_none_match, etag):
        return 304, None, headers

    return 200, response_body, headers


async def read_monitor_data_by_grant(
    grant_token: str,
    data_key: str,
    *,
    if_none_match: str | None = None,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    """Read realtime monitor JSON data via ``monitor_grant`` with live ACL re-check and ETag/304."""
    if not is_valid_data_key(data_key):
        raise HTTPException(status_code=400, detail="Invalid data key")

    monitor, _viewer_id = await _authorize_grant(grant_token)
    return await _read_monitor_data_response(
        monitor, data_key, if_none_match=if_none_match
    )


async def read_public_monitor_data(
    slug: str,
    data_key: str,
    *,
    if_none_match: str | None = None,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    """Anonymously read realtime monitor JSON data when ``visibility == 'public'``."""
    if not is_valid_data_key(data_key):
        raise HTTPException(status_code=400, detail="Invalid data key")
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if (
        monitor is None
        or monitor.visibility != "public"
        or not is_monitor_capability_enabled(monitor)
        or await _get_superseded_shared_document(monitor) is not None
    ):
        raise HTTPException(status_code=404, detail="Monitor not found")
    return await _read_monitor_data_response(
        monitor, data_key, if_none_match=if_none_match
    )


async def write_monitor_data_internal(
    slug: str,
    data_key: str,
    payload: Any,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    if not has_capability(CapabilityId.DATA_JOBS):
        raise HTTPException(status_code=404, detail="数据任务监控未启用")
    if not is_valid_data_key(data_key):
        raise HTTPException(status_code=400, detail="Invalid data key")
    monitor = await monitor_db.get_monitor_by_slug(slug)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控面板不存在")
    if monitor.source_kind != "data_job" or not monitor.source_ref:
        raise HTTPException(
            status_code=403,
            detail="仅 data_job 类型的监控面板支持内部实时数据写入",
        )
    binding = get_registered_monitor_binding_by_slug(monitor.source_ref, monitor.slug)
    if binding is None or data_key not in binding.data_keys:
        raise HTTPException(
            status_code=403,
            detail=f"data_key '{data_key}' 未在监控面板数据任务白名单中",
        )
    entry: MonitorDataEntry = await monitor_db.upsert_monitor_data(
        monitor.monitor_id,
        data_key,
        payload,
        as_of=as_of,
    )
    return {
        "monitor_id": entry.monitor_id,
        "slug": monitor.slug,
        "key": entry.data_key,
        "as_of": entry.as_of.isoformat(),
        "sha256": entry.sha256,
        "updated_at": entry.updated_at.isoformat(),
    }
