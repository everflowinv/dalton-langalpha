"""Non-HTML workspace document sharing (md, pdf, xlsx, etc.) without monitor panel entries.

Non-HTML files share the same 4-tier visibility model (``own``, ``members``,
``company``, ``public``) as Dalton Monitors, stored separately in
``dalton_shared_documents`` so they never create ``dalton_monitors`` rows.
Existing legacy non-HTML rows in ``dalton_monitors`` are surfaced read-only in
the shared-documents listing without deleting or migrating them destructively.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.config.env import PREVIEW_PROXY_FRAME_ANCESTORS
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth_guards import (
    raise_admin_required,
    raise_external_member_forbidden,
)
from src.dalton.company_identity import is_company_system_user
from src.dalton.monitors import db as monitor_db
from src.dalton.monitors.grants import (
    MonitorGrantError,
    doc_frame_prefix,
    mint_shared_document_grant,
    verify_shared_document_grant,
)
from src.dalton.monitors.models import (
    MonitorRecord,
    MonitorVisibility,
    SharedDocumentRecord,
    ShareDocumentRequest,
    UpdateSharedDocumentRequest,
    is_html_entry_path,
)
from src.dalton.monitors.permissions import (
    EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE,
    can_manage_monitor_fields,
    can_view_monitor,
    is_company_tier,
    require_monitor_publish_visibility,
    validate_publish_permissions,
)
from src.dalton.settings import is_artifact_publishing_enabled
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import records as artifacts_records_db
from src.server.database.artifacts import versions as artifacts_versions_db
from src.server.database.artifacts.versions import Origin, PublishResult, ReadBasis, VersionFile
from src.server.database.pool import get_db_connection
from src.server.database.share_codes import share_path
from src.server.database.workspace import get_workspace as db_get_workspace
from src.server.database.workspace_file_blobs import fetch_blob
from src.server.preview_proxy.artifacts import (
    ArtifactFileUnavailable,
    ServedArtifactFile,
    _content_type,
    _etag_matches,
    _headers,
    _valid_path,
)
from src.server.services.artifacts import snapshot as artifact_snapshot
from src.server.services.artifacts.manifest import REASON_LOADED
from src.server.services.artifacts.reader import ReadMode
from src.server.services.file_grants import seconds_left
from src.server.services.served_paths import may_be_artifact
from src.server.services.share_audience import ORGANIZATION
from src.server.services.workspace_file_containment import contained_relative_path
from src.server.services.workspace_files import (
    owned_workspace_on_computer,
    owner_work_dir,
    previous_dir_names_of,
)

logger = logging.getLogger(__name__)

_SHARED_DOC_SELECT = """
    SELECT
        d.doc_id::text AS doc_id,
        d.artifact_id::text AS artifact_id,
        d.owner_user_id,
        d.title,
        d.visibility,
        d.published_seq,
        d.keep_updated,
        d.acl_revision,
        d.created_at,
        d.updated_at,
        l.code AS artifact_code,
        l.workspace_id::text AS workspace_id,
        l.path AS entry_path,
        u.name AS owner_name,
        u.email AS owner_email,
        COALESCE(
            (
                SELECT ARRAY_AGG(dm.user_id ORDER BY dm.user_id)
                  FROM dalton_shared_document_members dm
                 WHERE dm.doc_id = d.doc_id
            ),
            ARRAY[]::varchar[]
        ) AS member_user_ids
      FROM dalton_shared_documents d
      JOIN share_links l ON l.artifact_id = d.artifact_id
      JOIN workspaces w ON w.workspace_id = l.workspace_id AND w.status <> 'deleted'
      LEFT JOIN users u ON u.user_id = d.owner_user_id
"""


def _row_to_record(row: dict[str, Any] | None) -> SharedDocumentRecord | None:
    if row is None:
        return None
    return SharedDocumentRecord(
        doc_id=str(row["doc_id"]),
        artifact_id=str(row["artifact_id"]),
        owner_user_id=str(row["owner_user_id"]),
        title=str(row["title"]),
        visibility=row["visibility"],
        published_seq=int(row["published_seq"]),
        keep_updated=bool(row["keep_updated"]),
        acl_revision=int(row["acl_revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        artifact_code=row.get("artifact_code"),
        workspace_id=row.get("workspace_id"),
        entry_path=row.get("entry_path"),
        owner_name=row.get("owner_name"),
        owner_email=row.get("owner_email"),
        member_user_ids=tuple(row.get("member_user_ids") or ()),
    )


def _legacy_monitor_to_doc(monitor: MonitorRecord) -> SharedDocumentRecord:
    return SharedDocumentRecord(
        doc_id=monitor.monitor_id,
        artifact_id=monitor.artifact_id,
        owner_user_id=monitor.owner_user_id,
        title=monitor.title,
        visibility=monitor.visibility,
        published_seq=monitor.published_seq,
        keep_updated=monitor.keep_updated,
        acl_revision=monitor.acl_revision,
        created_at=monitor.created_at,
        updated_at=monitor.updated_at,
        artifact_code=monitor.artifact_code,
        workspace_id=monitor.workspace_id,
        entry_path=monitor.entry_path,
        owner_name=monitor.owner_name,
        owner_email=monitor.owner_email,
        member_user_ids=monitor.member_user_ids,
    )


async def list_all_shared_documents() -> list[SharedDocumentRecord]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"{_SHARED_DOC_SELECT} ORDER BY d.updated_at DESC")
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


async def get_shared_document_by_artifact_id(
    artifact_id: str,
) -> SharedDocumentRecord | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"{_SHARED_DOC_SELECT} WHERE d.artifact_id = %s::uuid",
                (artifact_id,),
            )
            return _row_to_record(await cur.fetchone())


async def _select_doc_in_cur(cur: Any, doc_id: str) -> SharedDocumentRecord | None:
    await cur.execute(
        f"{_SHARED_DOC_SELECT} WHERE d.doc_id = %s::uuid",
        (doc_id,),
    )
    return _row_to_record(await cur.fetchone())


async def downgrade_public_shared_document_in_cur(cur: Any, artifact_id: str) -> None:
    """Downgrade any shared document row on *artifact_id* to ``own`` inside an open tx.

    Clears explicit member grants and bumps ``acl_revision`` so any previously
    minted ``d1.`` grants are immediately rejected on their next request.
    """
    await cur.execute(
        """
        SELECT doc_id::text AS doc_id, visibility
          FROM dalton_shared_documents
         WHERE artifact_id = %s::uuid
           FOR UPDATE
        """,
        (artifact_id,),
    )
    row = await cur.fetchone()
    if row is not None and row.get("visibility") != "own":
        await cur.execute(
            """
            UPDATE dalton_shared_documents
               SET visibility = 'own',
                   acl_revision = acl_revision + 1,
                   updated_at = NOW()
             WHERE doc_id = %s::uuid
            """,
            (row["doc_id"],),
        )
        await cur.execute(
            "DELETE FROM dalton_shared_document_members WHERE doc_id = %s::uuid",
            (row["doc_id"],),
        )


async def list_workspace_auto_update_shared_documents(
    workspace_id: str,
) -> list[SharedDocumentRecord]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                {_SHARED_DOC_SELECT}
                WHERE d.keep_updated = TRUE
                  AND l.workspace_id = %s::uuid
                ORDER BY d.doc_id
                """,
                (workspace_id,),
            )
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


async def _deactivate_legacy_non_html_monitor_in_cur(
    cur: Any, artifact_id: str, *, entry_path: str | None = None
) -> None:
    """Deactivate a legacy non-HTML ``dalton_monitors`` row's ACL/auto-update in-tx.

    Preserves the historical monitor row, its versions, and its data while
    immediately revoking old audience access and ``m1.`` grants.
    """
    if is_html_entry_path(entry_path):
        return
    await cur.execute(
        """
        SELECT monitor_id::text AS monitor_id, visibility, keep_updated
          FROM dalton_monitors
         WHERE artifact_id = %s::uuid
           FOR UPDATE
        """,
        (artifact_id,),
    )
    mon = await cur.fetchone()
    if mon is None:
        return
    mon_id = mon["monitor_id"]
    await cur.execute(
        "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
        (mon_id,),
    )
    await cur.execute(
        """
        UPDATE dalton_monitors
           SET visibility = 'own',
               keep_updated = FALSE,
               acl_revision = acl_revision + 1,
               updated_at = NOW()
         WHERE monitor_id = %s::uuid
        """,
        (mon_id,),
    )


SHARED_DOCUMENT_TAKEOVER_KEEP_UPDATED_DETAIL = (
    "该文档已由分享文档接管，请在分享设置中调整自动更新"
)


async def is_artifact_managed_by_shared_document_in_cur(
    cur: Any, artifact_id: str, *, entry_path: str | None = None
) -> bool:
    """Return True when *artifact_id* is managed by ``dalton_shared_documents`` inside an open tx."""
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled() or is_html_entry_path(entry_path):
        return False
    await cur.execute(
        """
        SELECT 1
          FROM dalton_shared_documents
         WHERE artifact_id = %s::uuid
           FOR SHARE
        """,
        (artifact_id,),
    )
    return await cur.fetchone() is not None


async def refuse_keep_updated_if_managed_by_shared_document(
    artifact_id: str, *, entry_path: str | None = None
) -> None:
    """Refuse enabling legacy ``share_links.keep_updated`` once managed by shared documents."""
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled() or is_html_entry_path(entry_path):
        return
    if await get_shared_document_by_artifact_id(artifact_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=SHARED_DOCUMENT_TAKEOVER_KEEP_UPDATED_DETAIL,
        )


async def _disable_legacy_share_link_auto_update_in_cur(
    cur: Any, locked: Any, *, workspace_owner_id: str
) -> Any:
    """Turn off legacy ``share_links.keep_updated`` in-tx so only shared-document turns publish."""
    if not getattr(locked, "keep_updated", False):
        return locked
    return await artifacts_records_db.set_record_in_cur(
        cur,
        user_id=workspace_owner_id,
        artifact_id=locked.artifact_id,
        assignments="keep_updated = FALSE",
        params={},
    )


async def downgrade_dalton_share_on_unshare_in_cur(
    cur: Any, artifact_id: str, *, entry_path: str | None = None
) -> None:
    """Downgrade public monitors, legacy non-HTML monitors, and shared documents to ``own``."""
    await cur.execute(
        """
        SELECT monitor_id::text AS monitor_id, visibility, keep_updated
          FROM dalton_monitors
         WHERE artifact_id = %s::uuid
           FOR UPDATE
        """,
        (artifact_id,),
    )
    mon = await cur.fetchone()
    mon_vis = mon.get("visibility") if mon is not None else None
    is_html = is_html_entry_path(entry_path)
    if mon is not None:
        if is_html and mon_vis == "public":
            await cur.execute(
                """
                UPDATE dalton_monitors
                   SET visibility = 'own',
                       acl_revision = acl_revision + 1,
                       updated_at = NOW()
                 WHERE monitor_id = %s::uuid
                """,
                (mon["monitor_id"],),
            )
        elif not is_html:
            await cur.execute(
                "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                (mon["monitor_id"],),
            )
            if mon_vis != "own" or bool(mon.get("keep_updated")):
                await cur.execute(
                    """
                    UPDATE dalton_monitors
                       SET visibility = 'own',
                           keep_updated = FALSE,
                           acl_revision = acl_revision + 1,
                           updated_at = NOW()
                     WHERE monitor_id = %s::uuid
                    """,
                    (mon["monitor_id"],),
                )
    await downgrade_public_shared_document_in_cur(cur, artifact_id)


async def can_view_shared_document(
    doc: SharedDocumentRecord,
    user_id: str | None,
    *,
    member_user_ids: Collection[str] | None = None,
) -> bool:
    """Apply the same 4-tier access control rules used by Dalton Monitors."""
    uid = str(user_id or "").strip()
    if not uid:
        return False

    visibility = doc.visibility
    if visibility == "public":
        return True
    if visibility == "own":
        return uid == doc.owner_user_id
    if visibility == "company":
        tier = await get_user_tier(uid)
        return is_company_tier(tier)
    if visibility == "members":
        if uid == doc.owner_user_id:
            return True
        members = member_user_ids if member_user_ids is not None else doc.member_user_ids
        if uid not in members:
            return False
        tier = await get_user_tier(uid)
        return is_company_tier(tier)
    return False


def _file_name_of(entry_path: str | None, fallback: str) -> str:
    clean = str(entry_path or "").strip()
    if clean:
        return clean.rsplit("/", 1)[-1]
    return fallback


def serialize_shared_document(
    doc: SharedDocumentRecord,
    *,
    can_manage: bool = False,
    viewer_id: str | None = None,
) -> dict[str, Any]:
    file_name = _file_name_of(doc.entry_path, doc.title)
    url = share_path(doc.artifact_code) if doc.artifact_code else ""
    is_owner = bool(viewer_id) and str(viewer_id).strip() == doc.owner_user_id
    return {
        "doc_id": doc.doc_id,
        "artifact_id": doc.artifact_id,
        "artifact_code": doc.artifact_code,
        "workspace_id": doc.workspace_id,
        "entry_path": doc.entry_path or file_name,
        "file_name": file_name,
        "title": doc.title or file_name,
        "owner_user_id": doc.owner_user_id,
        "owner_name": doc.owner_name,
        "owner_email": doc.owner_email,
        "visibility": doc.visibility,
        "member_user_ids": list(doc.member_user_ids),
        "published_seq": doc.published_seq,
        "keep_updated": doc.keep_updated,
        "acl_revision": doc.acl_revision,
        "url": url,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "can_manage": can_manage,
        "is_owner": is_owner,
    }


def _can_manage_doc(doc: SharedDocumentRecord, user_id: str, tier: str) -> bool:
    return can_manage_monitor_fields(
        owner_user_id=doc.owner_user_id,
        visibility=doc.visibility,
        caller_id=user_id,
        caller_tier=tier,
    )


async def _find_record_or_legacy_monitor(
    artifact_id: str,
) -> tuple[SharedDocumentRecord | None, MonitorRecord | None]:
    doc = await get_shared_document_by_artifact_id(artifact_id)
    if doc is not None:
        return doc, None
    legacy = await monitor_db.get_monitor_by_artifact_id(artifact_id)
    if (
        legacy is not None
        and legacy.source_kind == "workspace"
        and not is_html_entry_path(legacy.entry_path)
    ):
        return _legacy_monitor_to_doc(legacy), legacy
    return None, None


async def list_shared_documents_for_user(user_id: str) -> list[dict[str, Any]]:
    """List non-HTML documents shared by or shared with *user_id* that they may view.

    Excludes ``visibility == 'own'`` (not shared with others) and any HTML entry.
    Includes existing legacy non-HTML rows from ``dalton_monitors`` without
    mutating them, deduplicated by ``artifact_id``. Owners always see their own
    shared documents so they can stop sharing them even if their tier changed.
    """
    uid = str(user_id or "").strip()
    tier = await get_user_tier(uid)
    docs = await list_all_shared_documents()
    legacy_monitors = await monitor_db.list_all_monitors()

    visible_records: list[SharedDocumentRecord] = []
    # Any artifact that has a current dalton_shared_documents record is governed
    # solely by that record (even if narrowed to 'own' or a subset of members).
    superseded_artifacts: set[str] = {doc.artifact_id for doc in docs}
    seen_artifacts: set[str] = set()

    for doc in docs:
        if is_html_entry_path(doc.entry_path):
            continue
        if doc.visibility == "own" or doc.published_seq <= 0:
            continue
        if not doc.artifact_code:
            continue
        if doc.owner_user_id == uid or await can_view_shared_document(doc, uid):
            visible_records.append(doc)
            seen_artifacts.add(doc.artifact_id)

    for mon in legacy_monitors:
        if mon.source_kind != "workspace" or is_html_entry_path(mon.entry_path):
            continue
        if mon.artifact_id in superseded_artifacts or mon.artifact_id in seen_artifacts:
            continue
        if mon.visibility == "own" or mon.published_seq <= 0:
            continue
        if not mon.artifact_code:
            continue
        if mon.owner_user_id == uid or await can_view_monitor(mon, uid):
            visible_records.append(_legacy_monitor_to_doc(mon))
            seen_artifacts.add(mon.artifact_id)

    visible_records.sort(key=lambda item: item.updated_at, reverse=True)
    return [
        serialize_shared_document(
            item,
            can_manage=_can_manage_doc(item, uid, tier),
            viewer_id=uid,
        )
        for item in visible_records
    ]


async def list_grouped_shared_documents_for_user(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return shared non-HTML documents split into ``shared_by_me`` and ``shared_with_me``."""
    items = await list_shared_documents_for_user(user_id)
    shared_by_me = [item for item in items if item.get("is_owner")]
    shared_with_me = [item for item in items if not item.get("is_owner")]
    return {
        "documents": items,
        "shared_by_me": shared_by_me,
        "shared_with_me": shared_with_me,
    }


async def get_shared_document_by_artifact(
    artifact_id: str, *, viewer_id: str
) -> dict[str, Any] | None:
    doc, _legacy = await _find_record_or_legacy_monitor(artifact_id)
    if doc is None or is_html_entry_path(doc.entry_path):
        return None
    if str(viewer_id or "").strip() == doc.owner_user_id:
        return serialize_shared_document(doc, can_manage=True)
    if not await can_view_shared_document(doc, viewer_id):
        raise HTTPException(status_code=403, detail="无权访问此分享文档")
    tier = await get_user_tier(viewer_id)
    return serialize_shared_document(doc, can_manage=_can_manage_doc(doc, viewer_id, tier))


def _normalize_document_path(workspace: dict[str, Any], raw_path: str) -> str:
    work_dir = owner_work_dir(workspace)
    prev_dirs = previous_dir_names_of(workspace)
    clean = str(raw_path or "").strip().rstrip("/")
    relative = contained_relative_path(clean, work_dir, prev_dirs)
    if relative is None or not may_be_artifact(relative):
        raise HTTPException(status_code=400, detail="无效的工作区文件路径")
    if is_html_entry_path(relative):
        raise HTTPException(status_code=400, detail="网页文件请通过监控面板发布")
    return relative


async def commit_shared_document_publish_tx(
    *,
    artifact_id: str,
    workspace_owner_id: str,
    doc_owner_id: str,
    title: str,
    visibility: MonitorVisibility,
    keep_updated: bool,
    member_user_ids: Sequence[str],
    files: list[VersionFile],
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
    reject_if_pending_outbound_confirm: bool = False,
) -> tuple[SharedDocumentRecord, PublishResult]:
    """Atomically upsert ``dalton_shared_documents``, reconcile ``share_links``, and cut version."""
    clean_members = list(
        dict.fromkeys(str(m).strip() for m in member_user_ids if str(m).strip())
    )
    share_public = visibility == "public"
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                if read_basis is not None:
                    read_basis.check(locked)
                if reject_if_pending_outbound_confirm:
                    from src.dalton.settings import is_dalton_sharing_auth_enabled

                    if is_dalton_sharing_auth_enabled():
                        from src.dalton.colleague_outbound_guard import (
                            WorkspaceAutoPublishPaused,
                            is_workspace_pending_outbound_confirm_in_cur,
                        )

                        if await is_workspace_pending_outbound_confirm_in_cur(
                            cur, locked.workspace_id
                        ):
                            raise WorkspaceAutoPublishPaused(locked.workspace_id)

                await _deactivate_legacy_non_html_monitor_in_cur(
                    cur, artifact_id, entry_path=locked.path
                )
                await cur.execute(
                    """
                    SELECT doc_id::text AS doc_id, visibility, acl_revision
                      FROM dalton_shared_documents
                     WHERE artifact_id = %s::uuid
                       FOR UPDATE
                    """,
                    (artifact_id,),
                )
                existing = await cur.fetchone()
                live_tier = await monitor_db._resolve_live_tier_in_cur(cur, doc_owner_id)
                if (
                    existing is not None
                    and live_tier == "external"
                    and existing["visibility"] != "own"
                ):
                    raise_external_member_forbidden(
                        EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE
                    )
                if share_public and verify_public_authority is not None:
                    from src.dalton.monitors.service import (
                        PUBLIC_PERMISSION_LOST_MESSAGE,
                        PublicAuthorityLostError,
                    )
                    from src.dalton.settings import is_dalton_local_auth_enabled

                    if is_dalton_local_auth_enabled():
                        if live_tier != "admin":
                            raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
                    else:
                        await monitor_db._call_with_optional_conn(
                            verify_public_authority,
                            doc_owner_id,
                            conn=getattr(cur, "connection", None),
                        )
                require_monitor_publish_visibility(live_tier, visibility)

                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO dalton_shared_documents (
                            artifact_id, owner_user_id, title, visibility, keep_updated
                        )
                        VALUES (%s::uuid, %s, %s, %s, %s)
                        RETURNING doc_id::text AS doc_id
                        """,
                        (artifact_id, doc_owner_id, title, visibility, keep_updated),
                    )
                    inserted = await cur.fetchone()
                    doc_id = str(inserted["doc_id"])
                else:
                    doc_id = str(existing["doc_id"])
                    await cur.execute(
                        "SELECT user_id FROM dalton_shared_document_members WHERE doc_id = %s::uuid",
                        (doc_id,),
                    )
                    old_members = {str(r["user_id"]) for r in await cur.fetchall()}
                    acl_changed = (
                        existing["visibility"] != visibility
                        or (visibility == "members" and old_members != set(clean_members))
                        or (visibility != "members" and bool(old_members))
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_shared_documents
                           SET owner_user_id = %s,
                               title = %s,
                               visibility = %s,
                               keep_updated = %s,
                               acl_revision = acl_revision + %s,
                               updated_at = NOW()
                         WHERE doc_id = %s::uuid
                        """,
                        (
                            doc_owner_id,
                            title,
                            visibility,
                            keep_updated,
                            1 if acl_changed else 0,
                            doc_id,
                        ),
                    )

                await cur.execute(
                    "DELETE FROM dalton_shared_document_members WHERE doc_id = %s::uuid",
                    (doc_id,),
                )
                if visibility == "members" and clean_members:
                    await cur.executemany(
                        """
                        INSERT INTO dalton_shared_document_members (doc_id, user_id)
                        VALUES (%s::uuid, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [(doc_id, mid) for mid in clean_members],
                    )

                locked = await _disable_legacy_share_link_auto_update_in_cur(
                    cur, locked, workspace_owner_id=workspace_owner_id
                )
                if not share_public and locked.shared:
                    locked = await artifacts_records_db.unshare_in_cur(
                        cur, user_id=workspace_owner_id, artifact_id=artifact_id
                    )

                result = await artifacts_versions_db.cut_monitor_in_cur(
                    cur,
                    locked,
                    user_id=workspace_owner_id,
                    files=files,
                    title=title,
                    label=label,
                    share_public=share_public,
                    origin=origin,
                    loaded_files=loaded_files,
                    left_out_files=left_out_files,
                )
                await cur.execute(
                    """
                    UPDATE dalton_shared_documents
                       SET published_seq = %s,
                           updated_at = NOW()
                     WHERE doc_id = %s::uuid
                    """,
                    (result.seq, doc_id),
                )
                loaded = await _select_doc_in_cur(cur, doc_id)
    if loaded is None:
        raise RuntimeError(f"Shared document {doc_id} disappeared after commit")
    return loaded, result


async def update_shared_document_settings_tx(
    doc_id: str,
    *,
    artifact_id: str,
    owner_user_id: str,
    caller_id: str,
    title: str | None = None,
    visibility: MonitorVisibility | None = None,
    keep_updated: bool | None = None,
    member_user_ids: Sequence[str] | None = None,
) -> SharedDocumentRecord:
    clean_members: list[str] | None = (
        list(dict.fromkeys(str(m).strip() for m in member_user_ids if str(m).strip()))
        if member_user_ids is not None
        else None
    )
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=owner_user_id, artifact_id=artifact_id
                )
                await _deactivate_legacy_non_html_monitor_in_cur(
                    cur, artifact_id, entry_path=locked.path
                )
                locked = await _disable_legacy_share_link_auto_update_in_cur(
                    cur, locked, workspace_owner_id=owner_user_id
                )
                await cur.execute(
                    """
                    SELECT title, visibility, keep_updated, published_seq, owner_user_id
                      FROM dalton_shared_documents
                     WHERE doc_id = %s::uuid
                       FOR UPDATE
                    """,
                    (doc_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="分享文档不存在")

                cur_visibility: MonitorVisibility = row["visibility"]
                was_public = cur_visibility == "public"
                locked_owner = str(row["owner_user_id"])
                next_visibility: MonitorVisibility = (
                    visibility if visibility is not None else cur_visibility
                )
                caller_tier = await monitor_db._resolve_live_tier_in_cur(cur, caller_id)
                if not can_manage_monitor_fields(
                    owner_user_id=locked_owner,
                    visibility=cur_visibility,
                    caller_id=caller_id,
                    caller_tier=caller_tier,
                ):
                    raise_external_member_forbidden("只有文档所有者或管理员可修改分享设置")
                if caller_tier == "external" and next_visibility != "own":
                    raise_external_member_forbidden(
                        "外部成员分享文档时只能选择“仅自己”可见"
                    )
                if not was_public and next_visibility == "public" and caller_tier != "admin":
                    raise_admin_required("只有管理员可以将文档设为公开链接")

                next_title = (
                    title.strip() if title is not None and title.strip() else str(row["title"])
                )
                next_keep = (
                    keep_updated if keep_updated is not None else bool(row["keep_updated"])
                )
                published_seq = int(row["published_seq"] or 0)

                await cur.execute(
                    "SELECT user_id FROM dalton_shared_document_members WHERE doc_id = %s::uuid",
                    (doc_id,),
                )
                old_members = {str(r["user_id"]) for r in await cur.fetchall()}
                effective_members = (
                    clean_members if clean_members is not None else sorted(old_members)
                )
                if next_visibility == "members" and not effective_members:
                    raise HTTPException(
                        status_code=422,
                        detail="选择“指定同事”时请至少选择一位公司成员",
                    )

                acl_changed = (
                    cur_visibility != next_visibility
                    or (next_visibility == "members" and old_members != set(effective_members))
                    or (next_visibility != "members" and bool(old_members))
                )
                await cur.execute(
                    """
                    UPDATE dalton_shared_documents
                       SET title = %s,
                           visibility = %s,
                           keep_updated = %s,
                           acl_revision = acl_revision + %s,
                           updated_at = NOW()
                     WHERE doc_id = %s::uuid
                    """,
                    (
                        next_title,
                        next_visibility,
                        next_keep,
                        1 if acl_changed else 0,
                        doc_id,
                    ),
                )
                await cur.execute(
                    "DELETE FROM dalton_shared_document_members WHERE doc_id = %s::uuid",
                    (doc_id,),
                )
                if next_visibility == "members" and effective_members:
                    await cur.executemany(
                        """
                        INSERT INTO dalton_shared_document_members (doc_id, user_id)
                        VALUES (%s::uuid, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [(doc_id, mid) for mid in effective_members],
                    )
                if was_public and next_visibility != "public":
                    await artifacts_records_db.unshare_in_cur(
                        cur, user_id=owner_user_id, artifact_id=artifact_id
                    )
                elif not was_public and next_visibility == "public" and published_seq > 0:
                    await monitor_db._share_existing_version_publicly_in_cur(
                        cur, artifact_id, published_seq
                    )
                loaded = await _select_doc_in_cur(cur, doc_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="分享文档不存在")
    return loaded


async def share_workspace_document(
    payload: ShareDocumentRequest,
    *,
    caller_id: str,
    allow_clear_pause: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
) -> dict[str, Any]:
    """Share or update a non-HTML workspace file without creating a monitor entry."""
    from src.dalton.monitors import service as monitor_service

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

    rel_path = _normalize_document_path(workspace, payload.path)
    artifact = await artifacts_db.ensure_artifact(
        str(workspace["workspace_id"]), rel_path
    )
    existing, legacy_mon = await _find_record_or_legacy_monitor(artifact.artifact_id)

    is_downgrade_only = (
        existing is not None
        and existing.visibility == "public"
        and payload.visibility != "public"
    )
    if not is_artifact_publishing_enabled():
        if not is_downgrade_only:
            monitor_service._require_publishing_enabled()
        if existing is not None:
            if legacy_mon is not None:
                return await update_shared_document_settings(
                    artifact.artifact_id,
                    UpdateSharedDocumentRequest(
                        title=(payload.title or "").strip() or None,
                        visibility=payload.visibility,
                        keep_updated=payload.keep_updated,
                        member_user_ids=members if payload.visibility == "members" else [],
                    ),
                    caller_id=caller_id,
                ) | {"cut": False}
            updated_doc = await update_shared_document_settings_tx(
                existing.doc_id,
                artifact_id=existing.artifact_id,
                owner_user_id=caller_id,
                caller_id=caller_id,
                title=(payload.title or "").strip() or None,
                visibility=payload.visibility,
                keep_updated=payload.keep_updated,
                member_user_ids=members if payload.visibility == "members" else [],
            )
            tier = await get_user_tier(caller_id)
            return {
                "document": serialize_shared_document(
                    updated_doc, can_manage=_can_manage_doc(updated_doc, caller_id, tier)
                ),
                "cut": False,
            }

    if existing is not None:
        tier = await get_user_tier(caller_id)
        if not _can_manage_doc(existing, caller_id, tier):
            raise_external_member_forbidden("无权修改此分享文档")
        if tier == "external" and existing.visibility != "own":
            raise_external_member_forbidden(EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE)

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
        raise monitor_service._translate_snapshot_error(exc) from None

    effective_title = (payload.title or "").strip() or snap.title or title
    try:
        updated_doc, result = await commit_shared_document_publish_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=caller_id,
            doc_owner_id=caller_id,
            title=effective_title,
            visibility=payload.visibility,
            keep_updated=payload.keep_updated,
            member_user_ids=members,
            files=snap.files,
            label=payload.label,
            read_basis=ReadBasis.of(artifact),
            origin="owner",
            loaded_files=[e.path for e in snap.entries if e.reason == REASON_LOADED],
            left_out_files=[e.path for e in snap.left_out],
            verify_public_authority=monitor_service._verify_user_is_admin,
            reject_if_pending_outbound_confirm=reject_if_pending_outbound_confirm,
        )
    except Exception as exc:
        raise monitor_service._translate_snapshot_error(exc) from None

    tier = await get_user_tier(caller_id)
    response: dict[str, Any] = {
        "document": serialize_shared_document(
            updated_doc, can_manage=_can_manage_doc(updated_doc, caller_id, tier)
        ),
        "cut": result.cut,
    }
    if allow_clear_pause and str(caller_id) == str(workspace.get("user_id") or ""):
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled():
            from src.dalton.colleague_outbound_guard import clear_pending_outbound_confirm

            clear_res = await clear_pending_outbound_confirm(
                str(workspace["workspace_id"]),
                expected_pause_token=payload.confirm_pause_token,
            )
            if not clear_res.cleared and clear_res.status != "not_paused":
                response["auto_publish_paused"] = True
                response["auto_publish_pause_token"] = clear_res.current_pause_token
                response["auto_publish_pause_notice"] = clear_res.notice
    return response


async def update_shared_document_settings(
    artifact_id: str,
    payload: UpdateSharedDocumentRequest,
    *,
    caller_id: str,
) -> dict[str, Any]:
    from src.dalton.monitors import service as monitor_service
    from src.dalton.monitors.models import UpdateMonitorRequest

    doc, legacy_mon = await _find_record_or_legacy_monitor(artifact_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="分享文档不存在")

    if legacy_mon is not None:
        res = await monitor_service.update_monitor_settings(
            legacy_mon.slug,
            UpdateMonitorRequest(
                title=payload.title,
                visibility=payload.visibility,
                member_user_ids=payload.member_user_ids,
                keep_updated=payload.keep_updated,
            ),
            caller_id=caller_id,
        )
        mon = await monitor_db.get_monitor_by_slug(legacy_mon.slug)
        if mon is not None:
            tier = await get_user_tier(caller_id)
            converted = _legacy_monitor_to_doc(mon)
            return {
                "document": serialize_shared_document(
                    converted, can_manage=_can_manage_doc(converted, caller_id, tier)
                )
            }
        return {"document": res["monitor"]}

    tier = await get_user_tier(caller_id)
    if not _can_manage_doc(doc, caller_id, tier):
        raise_external_member_forbidden("只有文档所有者或管理员可修改分享设置")

    validated_members: list[str] | None = None
    if payload.visibility is not None or payload.member_user_ids is not None:
        if payload.visibility is not None:
            if tier == "external" and payload.visibility != "own":
                raise_external_member_forbidden("外部成员分享文档时只能选择“仅自己”可见")
            if payload.visibility == "public" and tier != "admin":
                raise_admin_required("只有管理员可以将文档设为公开链接")
            if payload.visibility == "public" and doc.visibility != "public":
                monitor_service._require_publishing_enabled()
        if payload.member_user_ids is not None:
            if tier == "external" and payload.member_user_ids:
                raise_external_member_forbidden("外部成员分享文档时只能选择“仅自己”可见")
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

    workspace_owner_id = doc.owner_user_id
    if doc.workspace_id:
        ws = await db_get_workspace(doc.workspace_id)
        if ws and ws.get("user_id"):
            workspace_owner_id = str(ws["user_id"])

    updated = await update_shared_document_settings_tx(
        doc.doc_id,
        artifact_id=doc.artifact_id,
        owner_user_id=workspace_owner_id,
        caller_id=caller_id,
        title=payload.title,
        visibility=payload.visibility,
        keep_updated=payload.keep_updated,
        member_user_ids=validated_members,
    )
    return {
        "document": serialize_shared_document(
            updated, can_manage=_can_manage_doc(updated, caller_id, tier)
        )
    }


async def resolve_shared_document_for_link(
    artifact_id: str, viewer_id: str | None
) -> str:
    """Check whether *viewer_id* may open an unshared ``/a/{code}`` link via Dalton document ACL.

    Returns ``"member"`` when permitted, ``"sign_in"`` when a signed-out visitor
    must sign in, or ``"denied"`` otherwise.
    """
    doc, _legacy = await _find_record_or_legacy_monitor(artifact_id)
    if doc is None or doc.published_seq <= 0:
        return "denied"
    if doc.visibility not in ("members", "company"):
        return "denied"
    if not viewer_id:
        return "sign_in"
    if await can_view_shared_document(doc, viewer_id):
        return "member"
    return "denied"


async def authorize_unshared_link_via_dalton_document(
    artifact_id: str | None,
    viewer_id: str | None,
    *,
    path: str | None = None,
) -> bool:
    """Thin hook for ``share_access.resolve_link`` when ``link.shared`` is False.

    Raises 401 ``members_only`` for signed-out visitors of colleague/team documents,
    returns ``True`` for authorized signed-in viewers, and ``False`` otherwise.
    """
    from src.dalton.settings import is_dalton_enabled

    if not artifact_id or not is_dalton_enabled() or is_html_entry_path(path):
        return False
    verdict = await resolve_shared_document_for_link(artifact_id, viewer_id)
    if verdict == "sign_in":
        raise HTTPException(
            status_code=401,
            detail={
                "type": "members_only",
                "message": "Sign in to open this link. It opens for authorized team members.",
            },
        )
    return verdict == "member"


async def build_shared_document_file_metadata(
    artifact_id: str, viewer_id: str
) -> dict[str, Any]:
    """Build ``SharedPublicFileMetadata`` for a Dalton-authorized non-HTML document viewer."""
    doc, _legacy = await _find_record_or_legacy_monitor(artifact_id)
    if doc is None or doc.published_seq <= 0 or not await can_view_shared_document(doc, viewer_id):
        raise HTTPException(status_code=404, detail="Shared thread not found")
    version = await artifacts_db.get_version(doc.artifact_id, doc.published_seq)
    if version is None:
        raise HTTPException(status_code=503, detail="File unavailable")
    grant = await mint_shared_document_grant(
        doc.artifact_id, viewer_id, doc.acl_revision
    )
    entry = version.entry_path or doc.entry_path or ""
    return {
        "kind": "file",
        "access": "public",
        "audience": ORGANIZATION,
        "artifact_id": doc.artifact_id,
        "name": entry.rsplit("/", 1)[-1],
        "path": entry,
        "title": version.title or doc.title,
        "seq": version.seq,
        "frame_base": doc_frame_prefix(grant),
        "isolated": False,
        "expires_in": seconds_left(grant.expires_at),
    }


async def serve_shared_document_file(
    grant_token: str,
    seq: str,
    path: str,
    *,
    if_none_match: str | None = None,
    head: bool = False,
) -> ServedArtifactFile:
    """Serve a shared non-HTML document's stored version file under live ACL verification."""
    from src.server.services import account as accounts

    try:
        verified = await verify_shared_document_grant(grant_token)
    except MonitorGrantError:
        raise HTTPException(status_code=403, detail="分享文档授权无效或已过期") from None

    if (
        await accounts.read_standing(verified.user_id, unknown_on_cache_error=True)
    ).suspended:
        raise HTTPException(status_code=403, detail="分享文档授权无效或已过期")

    doc, _legacy = await _find_record_or_legacy_monitor(verified.artifact_id)
    if doc is None or doc.published_seq <= 0:
        raise HTTPException(status_code=404, detail="File not found")

    if (
        not is_company_system_user(doc.owner_user_id)
        and (
            await accounts.read_standing(
                doc.owner_user_id, unknown_on_cache_error=True
            )
        ).suspended
    ):
        raise HTTPException(status_code=403, detail="分享文档授权无效或已过期")

    if doc.acl_revision != verified.acl_revision:
        raise HTTPException(status_code=403, detail="分享文档权限已变更，授权已失效")

    if not await can_view_shared_document(doc, verified.user_id):
        raise HTTPException(status_code=403, detail="无权访问此分享文档")

    if (
        not seq.isdigit()
        or int(seq) < 1
        or int(seq) != doc.published_seq
        or not _valid_path(path)
    ):
        raise HTTPException(status_code=404, detail="File not found")

    stored = await artifacts_db.get_version_file(doc.artifact_id, int(seq), path)
    if stored is None:
        raise HTTPException(status_code=404, detail="File not found")

    content_type = _content_type(stored.content_type)
    etag = stored.sha256
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
        headers.append(("content-length", str(stored.byte_len)))
        return ServedArtifactFile(body=b"", headers=headers)

    if stored.content is not None:
        body = stored.content
    else:
        try:
            body = await fetch_blob(stored.user_id, stored.sha256)
        except Exception as exc:
            raise ArtifactFileUnavailable(str(exc)) from exc

    headers.append(("content-length", str(len(body))))
    return ServedArtifactFile(body=body, headers=headers)


async def commit_shared_document_version_tx(
    *,
    doc_id: str,
    artifact_id: str,
    workspace_owner_id: str,
    files: list[VersionFile],
    title: str | None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "keep_updated",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
    check_pending_outbound_confirm: bool = False,
) -> tuple[SharedDocumentRecord, PublishResult | None]:
    """Lock ``share_links`` + ``dalton_shared_documents``, re-check ACL/pause, and cut atomically."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                await _deactivate_legacy_non_html_monitor_in_cur(
                    cur, artifact_id, entry_path=locked.path
                )
                await cur.execute(
                    """
                    SELECT doc_id::text AS doc_id,
                           visibility,
                           owner_user_id,
                           title,
                           published_seq,
                           keep_updated
                      FROM dalton_shared_documents
                     WHERE doc_id = %s::uuid
                       FOR UPDATE
                    """,
                    (doc_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise LookupError(f"Shared document {doc_id} not found")
                if not bool(row.get("keep_updated")):
                    existing = await _select_doc_in_cur(cur, doc_id)
                    if existing is None:
                        raise LookupError(f"Shared document {doc_id} not found")
                    return existing, None

                if check_pending_outbound_confirm:
                    from src.dalton.settings import is_dalton_sharing_auth_enabled

                    if is_dalton_sharing_auth_enabled():
                        from src.dalton.colleague_outbound_guard import (
                            is_workspace_pending_outbound_confirm_in_cur,
                        )

                        if await is_workspace_pending_outbound_confirm_in_cur(
                            cur, locked.workspace_id
                        ):
                            existing = await _select_doc_in_cur(cur, doc_id)
                            if existing is None:
                                raise LookupError(f"Shared document {doc_id} not found")
                            return existing, None

                live_visibility: MonitorVisibility = row["visibility"]
                locked_owner = str(row["owner_user_id"])
                share_public = live_visibility == "public"

                from src.dalton.settings import is_dalton_local_auth_enabled

                live_tier = await monitor_db._resolve_live_tier_in_cur(cur, locked_owner)
                if live_tier == "external" and live_visibility != "own":
                    require_monitor_publish_visibility(live_tier, live_visibility)
                if share_public and verify_public_authority is not None:
                    if is_dalton_local_auth_enabled():
                        if live_tier != "admin":
                            from src.dalton.monitors.service import (
                                PUBLIC_PERMISSION_LOST_MESSAGE,
                                PublicAuthorityLostError,
                            )

                            raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
                    else:
                        await monitor_db._call_with_optional_conn(
                            verify_public_authority,
                            locked_owner,
                            conn=getattr(cur, "connection", None),
                        )
                require_monitor_publish_visibility(live_tier, live_visibility)

                if read_basis is not None:
                    read_basis.check(locked)

                locked = await _disable_legacy_share_link_auto_update_in_cur(
                    cur, locked, workspace_owner_id=workspace_owner_id
                )
                if not share_public and locked.shared:
                    locked = await artifacts_records_db.unshare_in_cur(
                        cur, user_id=workspace_owner_id, artifact_id=artifact_id
                    )

                result = await artifacts_versions_db.cut_monitor_in_cur(
                    cur,
                    locked,
                    user_id=workspace_owner_id,
                    files=files,
                    title=title or str(row["title"]),
                    share_public=share_public,
                    origin=origin,
                    loaded_files=loaded_files,
                    left_out_files=left_out_files,
                )
                await cur.execute(
                    """
                    UPDATE dalton_shared_documents
                       SET published_seq = %s,
                           updated_at = NOW()
                     WHERE doc_id = %s::uuid
                    """,
                    (result.seq, doc_id),
                )
                loaded = await _select_doc_in_cur(cur, doc_id)
    if loaded is None:
        raise LookupError(f"Shared document {doc_id} not found")
    return loaded, result


async def republish_workspace_shared_documents_after_turn(*, workspace_id: str) -> None:
    """Refresh every workspace shared document with ``keep_updated=TRUE`` after a turn ends.

    Reuses the same publishing switch, live owner permission re-check, colleague
    outbound pause guard, and absent-dependency protection as workspace monitors.
    """
    from src.dalton.monitors import service as monitor_service

    if not is_artifact_publishing_enabled():
        return
    docs = await list_workspace_auto_update_shared_documents(workspace_id)
    if not docs:
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
                "Skipping auto-update for workspace %s shared documents: pending_outbound_confirm",
                workspace_id,
            )
            return

    for doc in docs:
        if sharing_auth_enabled:
            from src.dalton.colleague_outbound_guard import (
                is_workspace_pending_outbound_confirm,
            )

            try:
                fresh_ws = await db_get_workspace(workspace_id)
            except Exception:
                logger.warning(
                    "Skipping auto-update for shared document %s: failed to re-read workspace %s",
                    doc.doc_id,
                    workspace_id,
                    exc_info=True,
                )
                continue
            if not fresh_ws or is_workspace_pending_outbound_confirm(fresh_ws):
                logger.info(
                    "Skipping auto-update for shared document %s: pending_outbound_confirm",
                    doc.doc_id,
                )
                continue
        try:
            owner_tier = await get_user_tier(doc.owner_user_id)
            if owner_tier == "external" and doc.visibility != "own":
                require_monitor_publish_visibility(owner_tier, doc.visibility)
            if doc.visibility == "public" and owner_tier != "admin":
                raise monitor_service.PublicAuthorityLostError(
                    monitor_service.PUBLIC_PERMISSION_LOST_MESSAGE
                )
            require_monitor_publish_visibility(owner_tier, doc.visibility)
            artifact = await artifacts_db.get_artifact(
                doc.artifact_id, user_id=workspace_owner_id
            )
            if artifact is None:
                continue
            snap = await artifact_snapshot.capture_workspace_monitor_snapshot(
                artifact,
                user_id=workspace_owner_id,
                mode=ReadMode.WARM_ONLY,
            )
            await monitor_service._abort_if_absent_dependencies(
                doc.artifact_id,
                doc.published_seq or artifact.latest_seq,
                snap,
            )
            await commit_shared_document_version_tx(
                doc_id=doc.doc_id,
                artifact_id=doc.artifact_id,
                workspace_owner_id=workspace_owner_id,
                files=snap.files,
                title=doc.title,
                read_basis=ReadBasis.of(artifact),
                origin="keep_updated",
                loaded_files=[
                    e.path for e in snap.entries if e.reason == REASON_LOADED
                ],
                verify_public_authority=monitor_service._verify_user_is_admin,
                check_pending_outbound_confirm=sharing_auth_enabled,
            )
        except Exception as exc:
            logger.warning(
                "Auto-updating shared document %s (%s) failed: %s",
                doc.doc_id,
                doc.artifact_id,
                exc,
                exc_info=True,
            )
