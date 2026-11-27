"""Database persistence for Dalton Monitors, members, data payloads, and version pruning."""

from __future__ import annotations

import hashlib
import inspect
import json
import posixpath
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, uuid5

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.access_tiers import get_user_tier
from src.dalton.company_identity import (
    COMPANY_SYSTEM_USER_ID,
    ensure_company_system_identity,
    is_company_system_user,
)
from src.dalton.monitors.models import (
    MAX_MONITOR_VERSIONS,
    CompanyMember,
    MonitorCategory,
    MonitorDataEntry,
    MonitorRecord,
    MonitorSourceKind,
    MonitorVisibility,
    is_html_entry_path,
    is_valid_slug,
    slugify_candidate,
)
from src.dalton.monitors.permissions import can_manage_monitor_fields
from src.server.database.artifacts import records as artifacts_records_db
from src.server.database.artifacts import versions as artifacts_versions_db
from src.server.database.artifacts.versions import (
    LeftOutChanged,
    Origin,
    PublishResult,
    ReadBasis,
    SharingChanged,
    VersionFile,
)
from src.server.database.pool import get_db_connection

COMPANY_MONITOR_WORKSPACE_ID = str(uuid5(NAMESPACE_DNS, "dalton.company.monitors"))

_MONITOR_SELECT = """
    SELECT
        m.monitor_id::text AS monitor_id,
        m.slug,
        m.artifact_id::text AS artifact_id,
        m.owner_user_id,
        m.title,
        m.category,
        m.sort_order,
        m.visibility,
        m.published_seq,
        m.keep_updated,
        m.source_kind,
        m.source_ref,
        m.acl_revision,
        m.last_publish_run_id,
        m.last_publish_error,
        m.last_publish_failed_at,
        m.last_verified_seq,
        m.last_verified_at,
        m.created_at,
        m.updated_at,
        l.code AS artifact_code,
        l.workspace_id::text AS workspace_id,
        l.path AS entry_path,
        u.name AS owner_name,
        u.email AS owner_email,
        (
            SELECT MAX(d.as_of)
              FROM dalton_monitor_data d
             WHERE d.monitor_id = m.monitor_id
        ) AS data_as_of,
        COALESCE(
            (
                SELECT ARRAY_AGG(mm.user_id ORDER BY mm.user_id)
                  FROM dalton_monitor_members mm
                 WHERE mm.monitor_id = m.monitor_id
            ),
            ARRAY[]::varchar[]
        ) AS member_user_ids
      FROM dalton_monitors m
      JOIN share_links l ON l.artifact_id = m.artifact_id
      JOIN workspaces w ON w.workspace_id = l.workspace_id AND w.status <> 'deleted'
      LEFT JOIN users u ON u.user_id = m.owner_user_id
"""


def _row_to_record(row: dict[str, Any] | None) -> MonitorRecord | None:
    if row is None:
        return None
    members = tuple(row.get("member_user_ids") or ())
    return MonitorRecord(
        monitor_id=str(row["monitor_id"]),
        slug=str(row["slug"]),
        artifact_id=str(row["artifact_id"]),
        owner_user_id=str(row["owner_user_id"]),
        title=str(row["title"]),
        category=row["category"],
        sort_order=int(row["sort_order"]),
        visibility=row["visibility"],
        published_seq=int(row["published_seq"]),
        keep_updated=bool(row["keep_updated"]),
        source_kind=row["source_kind"],
        source_ref=row.get("source_ref"),
        acl_revision=int(row["acl_revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_publish_run_id=row.get("last_publish_run_id"),
        last_publish_error=row.get("last_publish_error"),
        last_publish_failed_at=row.get("last_publish_failed_at"),
        last_verified_seq=(
            int(row["last_verified_seq"])
            if row.get("last_verified_seq") is not None
            else None
        ),
        last_verified_at=row.get("last_verified_at"),
        artifact_code=row.get("artifact_code"),
        workspace_id=row.get("workspace_id"),
        entry_path=row.get("entry_path"),
        owner_name=row.get("owner_name"),
        owner_email=row.get("owner_email"),
        data_as_of=row.get("data_as_of"),
        member_user_ids=members,
    )


async def ensure_company_monitor_workspace() -> str:
    """Idempotently ensure the company system user and its monitor workspace exist."""
    await ensure_company_system_identity()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, is_pinned)
                VALUES (%s, %s, %s, 'unbound', FALSE)
                ON CONFLICT (workspace_id) DO NOTHING
                """,
                (
                    COMPANY_MONITOR_WORKSPACE_ID,
                    COMPANY_SYSTEM_USER_ID,
                    "Dalton Company Monitors",
                ),
            )
    return COMPANY_MONITOR_WORKSPACE_ID


async def get_monitor_by_slug(slug: str) -> MonitorRecord | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"{_MONITOR_SELECT} WHERE m.slug = %s", (slug,))
            return _row_to_record(await cur.fetchone())


async def get_monitor_by_id(monitor_id: str) -> MonitorRecord | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"{_MONITOR_SELECT} WHERE m.monitor_id = %s::uuid", (monitor_id,)
            )
            return _row_to_record(await cur.fetchone())


async def get_monitor_by_artifact_id(artifact_id: str) -> MonitorRecord | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"{_MONITOR_SELECT} WHERE m.artifact_id = %s::uuid", (artifact_id,)
            )
            return _row_to_record(await cur.fetchone())


async def get_monitor_by_data_job(job_key: str) -> MonitorRecord | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"{_MONITOR_SELECT} WHERE m.source_kind = 'data_job' AND m.source_ref = %s",
                (job_key,),
            )
            return _row_to_record(await cur.fetchone())


async def list_monitors_by_data_job(job_key: str) -> list[MonitorRecord]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"{_MONITOR_SELECT} WHERE m.source_kind = 'data_job' AND m.source_ref = %s ORDER BY m.sort_order, m.slug",
                (job_key,),
            )
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


async def list_all_monitors() -> list[MonitorRecord]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                {_MONITOR_SELECT}
                ORDER BY m.category, m.sort_order, m.updated_at DESC
                """
            )
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


async def list_workspace_auto_update_monitors(workspace_id: str) -> list[MonitorRecord]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                {_MONITOR_SELECT}
                WHERE m.source_kind = 'workspace'
                  AND m.keep_updated = TRUE
                  AND l.workspace_id = %s::uuid
                  AND NOT EXISTS (
                      SELECT 1
                        FROM dalton_shared_documents d
                       WHERE d.artifact_id = m.artifact_id
                  )
                ORDER BY m.slug
                """,
                (workspace_id,),
            )
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


async def has_workspace_auto_update_monitors(workspace_id: str) -> bool:
    """Return True if ``workspace_id`` has any workspace monitor with ``keep_updated=TRUE``."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                  FROM dalton_monitors m
                  JOIN share_links l ON l.artifact_id = m.artifact_id
                 WHERE m.source_kind = 'workspace'
                   AND m.keep_updated = TRUE
                   AND l.workspace_id = %s::uuid
                 LIMIT 1
                """,
                (workspace_id,),
            )
            return await cur.fetchone() is not None


class MonitorSupersededBySharedDocumentError(HTTPException):
    """Raised when a legacy non-HTML monitor mutation is rejected after shared-document takeover."""

    def __init__(
        self,
        detail: str = "该文档已由分享文档接管，旧监控入口仅保留历史只读记录",
    ) -> None:
        super().__init__(status_code=403, detail=detail)


async def _reject_if_superseded_by_shared_document_in_cur(
    cur: Any, artifact_id: str, *, entry_path: str | None = None
) -> None:
    """Reject mutations on a legacy non-HTML monitor already taken over by ``dalton_shared_documents``."""
    if not entry_path or is_html_entry_path(entry_path):
        return
    await cur.execute(
        """
        SELECT 1
          FROM dalton_shared_documents
         WHERE artifact_id = %s::uuid
           FOR SHARE
        """,
        (artifact_id,),
    )
    if await cur.fetchone() is not None:
        raise MonitorSupersededBySharedDocumentError()


async def pick_available_slug(base_raw: str) -> str:
    """Return a unique, non-reserved slug derived from ``base_raw``."""
    base = slugify_candidate(base_raw)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            candidate = base
            suffix = 2
            while True:
                if is_valid_slug(candidate):
                    await cur.execute(
                        "SELECT 1 FROM dalton_monitors WHERE slug = %s", (candidate,)
                    )
                    if await cur.fetchone() is None:
                        return candidate
                tail = f"-{suffix}"
                candidate = f"{base[: 110 - len(tail)].rstrip('-')}{tail}"
                suffix += 1


async def upsert_monitor(
    *,
    slug: str,
    artifact_id: str,
    owner_user_id: str,
    title: str,
    category: MonitorCategory,
    sort_order: int,
    visibility: MonitorVisibility,
    keep_updated: bool,
    source_kind: MonitorSourceKind,
    source_ref: str | None,
    member_user_ids: Sequence[str],
    bump_acl_revision: bool = False,
) -> MonitorRecord:
    """Create or update a monitor row and replace its ``dalton_monitor_members`` list."""
    clean_members = list(dict.fromkeys(str(m).strip() for m in member_user_ids if str(m).strip()))
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id, visibility, acl_revision
                      FROM dalton_monitors
                     WHERE artifact_id = %s::uuid
                       FOR UPDATE
                    """,
                    (artifact_id,),
                )
                existing = await cur.fetchone()
                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO dalton_monitors (
                            slug, artifact_id, owner_user_id, title, category,
                            sort_order, visibility, keep_updated, source_kind, source_ref
                        )
                        VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING monitor_id::text AS monitor_id
                        """,
                        (
                            slug,
                            artifact_id,
                            owner_user_id,
                            title,
                            category,
                            sort_order,
                            visibility,
                            keep_updated,
                            source_kind,
                            source_ref,
                        ),
                    )
                    inserted = await cur.fetchone()
                    monitor_id = str(inserted["monitor_id"])
                else:
                    monitor_id = str(existing["monitor_id"])
                    await cur.execute(
                        """
                        SELECT user_id FROM dalton_monitor_members
                         WHERE monitor_id = %s::uuid
                        """,
                        (monitor_id,),
                    )
                    old_members = {str(r["user_id"]) for r in await cur.fetchall()}
                    acl_changed = (
                        bump_acl_revision
                        or existing["visibility"] != visibility
                        or old_members != set(clean_members)
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_monitors
                           SET title = %s,
                               category = %s,
                               sort_order = %s,
                               visibility = %s,
                               keep_updated = %s,
                               source_kind = %s,
                               source_ref = %s,
                               acl_revision = acl_revision + %s,
                               updated_at = NOW()
                         WHERE monitor_id = %s::uuid
                        """,
                        (
                            title,
                            category,
                            sort_order,
                            visibility,
                            keep_updated,
                            source_kind,
                            source_ref,
                            1 if acl_changed else 0,
                            monitor_id,
                        ),
                    )

                await cur.execute(
                    "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                    (monitor_id,),
                )
                if visibility == "members" and clean_members:
                    await cur.executemany(
                        """
                        INSERT INTO dalton_monitor_members (monitor_id, user_id)
                        VALUES (%s::uuid, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [(monitor_id, mid) for mid in clean_members],
                    )
    loaded = await get_monitor_by_id(monitor_id)
    if loaded is None:
        raise RuntimeError(f"Monitor {monitor_id} disappeared after upsert")
    return loaded


async def _select_monitor_in_cur(cur, monitor_id: str) -> MonitorRecord | None:
    await cur.execute(
        f"{_MONITOR_SELECT} WHERE m.monitor_id = %s::uuid", (monitor_id,)
    )
    return _row_to_record(await cur.fetchone())


async def _share_existing_version_publicly_in_cur(
    cur, artifact_id: str, seq: int
) -> None:
    await cur.execute(
        """
        UPDATE share_links l
           SET shared_at = COALESCE(l.shared_at, NOW()),
               shared_seq = %(seq)s,
               files = COALESCE(
                   (
                       SELECT ARRAY_AGG(f.path ORDER BY f.path)
                         FROM artifact_version_files f
                        WHERE f.artifact_id = %(artifact_id)s::uuid
                          AND f.seq = %(seq)s
                   ),
                   ARRAY[l.path]::text[]
               ),
               revision = l.revision + CASE WHEN l.shared_at IS NULL THEN 1 ELSE 0 END,
               legacy_refused = NULL
         WHERE l.artifact_id = %(artifact_id)s::uuid
           AND l.kind = 'file'
        """,
        {"artifact_id": artifact_id, "seq": seq},
    )


async def _record_monitor_version_in_cur(cur, monitor_id: str, seq: int) -> None:
    if seq <= 0:
        return
    await cur.execute(
        """
        INSERT INTO dalton_monitor_versions (monitor_id, seq)
        VALUES (%s::uuid, %s)
        ON CONFLICT (monitor_id, seq) DO NOTHING
        """,
        (monitor_id, seq),
    )


def _derive_version_entry_path(
    paths: Sequence[str],
    *,
    fallback_entry_path: str | None = None,
) -> str:
    """Derive a version's entry path from its stored file paths when ``artifact_versions.entry_path`` is missing."""
    fallback = str(fallback_entry_path or "").strip()
    clean_paths = [str(p).strip() for p in paths if str(p or "").strip()]
    if not clean_paths:
        return fallback
    if fallback and fallback in clean_paths:
        return fallback
    if fallback:
        fallback_name = posixpath.basename(fallback)
        if fallback_name in clean_paths:
            return fallback_name
        suffix = f"/{fallback_name}"
        matching = [p for p in clean_paths if p.endswith(suffix)]
        if matching:
            return min(matching, key=lambda p: (p.count("/"), p))
    if "index.html" in clean_paths:
        return "index.html"
    index_children = [p for p in clean_paths if p.endswith("/index.html")]
    if index_children:
        return min(index_children, key=lambda p: (p.count("/"), p))
    html_files = [p for p in clean_paths if p.lower().endswith((".html", ".htm"))]
    if html_files:
        return min(html_files, key=lambda p: (p.count("/"), len(p), p))
    return min(clean_paths)


async def list_monitor_versions(
    monitor_id: str,
    artifact_id: str,
    *,
    fallback_entry_path: str | None = None,
) -> list[artifacts_versions_db.Version]:
    """Return only versions explicitly published to ``monitor_id`` (excluding private kept cuts)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT v.seq, v.entry_path, v.origin, v.title, v.label, v.byte_len, v.published_at
                  FROM artifact_versions v
                  JOIN dalton_monitor_versions mv
                    ON mv.monitor_id = %s::uuid AND mv.seq = v.seq
                 WHERE v.artifact_id = %s::uuid
                 ORDER BY v.seq DESC
                """,
                (monitor_id, artifact_id),
            )
            rows = await cur.fetchall()
            missing_seqs = [
                int(row["seq"])
                for row in rows
                if not str(row.get("entry_path") or "").strip()
            ]
            files_by_seq: dict[int, list[str]] = {}
            if missing_seqs:
                await cur.execute(
                    """
                    SELECT seq, path
                      FROM artifact_version_files
                     WHERE artifact_id = %s::uuid
                       AND seq = ANY(%s)
                     ORDER BY seq ASC, path ASC
                    """,
                    (artifact_id, missing_seqs),
                )
                for file_row in await cur.fetchall():
                    files_by_seq.setdefault(int(file_row["seq"]), []).append(
                        str(file_row["path"])
                    )
    versions: list[artifacts_versions_db.Version] = []
    for row in rows:
        entry_path = str(row.get("entry_path") or "").strip()
        if not entry_path:
            entry_path = _derive_version_entry_path(
                files_by_seq.get(int(row["seq"]), ()),
                fallback_entry_path=fallback_entry_path,
            )
        versions.append(
            artifacts_versions_db.Version(
                seq=int(row["seq"]),
                entry_path=entry_path,
                origin=row["origin"],
                title=row.get("title"),
                label=row.get("label"),
                byte_len=int(row["byte_len"]),
                published_at=row["published_at"],
            )
        )
    return versions


async def is_monitor_version_published(monitor_id: str, seq: int) -> bool:
    """Return True if ``seq`` was explicitly published to ``monitor_id``."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                  FROM dalton_monitor_versions
                 WHERE monitor_id = %s::uuid AND seq = %s
                """,
                (monitor_id, seq),
            )
            return await cur.fetchone() is not None


async def has_processed_monitor_run(monitor_id: str, run_id: str) -> bool:
    """Return True if ``(monitor_id, run_id)`` was already committed successfully."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM dalton_monitor_publish_runs
                 WHERE monitor_id = %s::uuid AND run_id = %s
                """,
                (monitor_id, run_id),
            )
            return await cur.fetchone() is not None


async def update_monitor_settings_tx(
    monitor_id: str,
    *,
    artifact_id: str,
    owner_user_id: str,
    caller_id: str,
    title: str | None = None,
    category: MonitorCategory | None = None,
    sort_order: int | None = None,
    visibility: MonitorVisibility | None = None,
    keep_updated: bool | None = None,
    member_user_ids: Sequence[str] | None = None,
    require_publishing_enabled_fn: Callable[[], None] | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
    resolve_caller_tier: Callable[[str], Awaitable[str]] | None = None,
    reject_external_non_own_existing: bool = False,
    public_approver_id: str | None = None,
) -> MonitorRecord:
    """Update only explicitly provided monitor fields under lock, authorize in lock, and sync share_links atomically."""
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
                await cur.execute(
                    """
                    SELECT title, category, sort_order, visibility, keep_updated,
                           published_seq, owner_user_id, source_kind, source_ref, slug
                      FROM dalton_monitors
                     WHERE monitor_id = %s::uuid
                       FOR UPDATE
                    """,
                    (monitor_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise LookupError(f"Monitor {monitor_id} not found")
                if row["source_kind"] == "workspace":
                    await _reject_if_superseded_by_shared_document_in_cur(
                        cur, artifact_id, entry_path=getattr(locked, "path", None)
                    )

                cur_visibility: MonitorVisibility = row["visibility"]
                was_public = cur_visibility == "public"
                locked_owner = str(row["owner_user_id"])
                locked_source_kind: MonitorSourceKind = row["source_kind"]
                next_visibility: MonitorVisibility = (
                    visibility if visibility is not None else cur_visibility
                )
                from src.dalton.settings import is_dalton_local_auth_enabled

                approver_uid: str | None = None
                if locked_source_kind == "data_job" and next_visibility == "public":
                    approver_uid = str(public_approver_id or "").strip() or None
                    if approver_uid is None and row.get("source_ref") is not None:
                        from src.dalton.monitors.data_job_registry import (
                            get_registered_monitor_binding_by_slug,
                        )

                        binding = get_registered_monitor_binding_by_slug(
                            str(row.get("source_ref") or ""),
                            str(row.get("slug") or ""),
                        )
                        if binding is not None and binding.approved_by_admin_user_id:
                            approver_uid = (
                                binding.approved_by_admin_user_id.strip() or None
                            )

                cur_conn = getattr(cur, "connection", None)
                live_tiers: dict[str, str] = {}
                if resolve_caller_tier is not None and (
                    resolve_caller_tier is not get_user_tier
                    or not is_dalton_local_auth_enabled()
                ):
                    caller_tier = await _call_with_optional_conn(
                        resolve_caller_tier, caller_id, conn=cur_conn
                    )
                else:
                    lock_candidates = [caller_id, locked_owner]
                    if approver_uid is not None:
                        lock_candidates.append(approver_uid)
                    uids_to_lock = sorted(
                        {
                            str(u).strip()
                            for u in lock_candidates
                            if u
                            and str(u).strip()
                            and not is_company_system_user(str(u).strip())
                        }
                    )
                    for uid in uids_to_lock:
                        live_tiers[uid] = await _resolve_live_tier_in_cur(cur, uid)
                    caller_tier = (
                        "company"
                        if is_company_system_user(caller_id)
                        else live_tiers.get(str(caller_id).strip(), "external")
                    )
                if not can_manage_monitor_fields(
                    owner_user_id=locked_owner,
                    visibility=cur_visibility,
                    caller_id=caller_id,
                    caller_tier=caller_tier,
                ):
                    from src.dalton.auth_guards import raise_external_member_forbidden

                    raise_external_member_forbidden("只有面板所有者或管理员可修改设置")

                if (
                    reject_external_non_own_existing
                    and caller_tier == "external"
                    and cur_visibility != "own"
                ):
                    from src.dalton.auth_guards import raise_external_member_forbidden

                    raise_external_member_forbidden(
                        "外部成员发布监控面板时只能选择“仅自己”可见"
                    )

                if caller_tier == "external" and next_visibility != "own":
                    from src.dalton.auth_guards import raise_external_member_forbidden

                    raise_external_member_forbidden(
                        "外部成员发布监控面板时只能选择“仅自己”可见"
                    )
                if not was_public and next_visibility == "public" and caller_tier != "admin":
                    from src.dalton.auth_guards import raise_admin_required

                    raise_admin_required("只有管理员可以将监控面板设为公开链接")

                next_title = (
                    title.strip() if title is not None and title.strip() else str(row["title"])
                )
                next_category: MonitorCategory = (
                    category if category is not None else row["category"]
                )
                next_sort = (
                    sort_order if sort_order is not None else int(row["sort_order"])
                )
                next_keep = (
                    keep_updated if keep_updated is not None else bool(row["keep_updated"])
                )
                published_seq = int(row["published_seq"] or 0)
                takeover = (
                    was_public
                    and locked_source_kind == "workspace"
                    and caller_tier == "admin"
                    and caller_id != locked_owner
                )
                effective_owner = caller_id if takeover else locked_owner

                await cur.execute(
                    "SELECT user_id FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                    (monitor_id,),
                )
                old_members = {str(r["user_id"]) for r in await cur.fetchall()}
                effective_members = (
                    clean_members
                    if clean_members is not None
                    else sorted(old_members)
                )
                if next_visibility == "members" and not effective_members:
                    raise HTTPException(
                        status_code=422,
                        detail="选择“指定成员”时请至少选择一位公司成员",
                    )

                if not was_public and next_visibility == "public":
                    if require_publishing_enabled_fn is not None:
                        require_publishing_enabled_fn()
                if next_visibility == "public" and verify_public_authority is not None:
                    if locked_source_kind == "data_job":
                        if (
                            is_dalton_local_auth_enabled()
                            and approver_uid is not None
                            and approver_uid in live_tiers
                            and live_tiers[approver_uid] != "admin"
                        ):
                            from src.dalton.monitors.service import (
                                PUBLIC_PERMISSION_LOST_MESSAGE,
                                PublicAuthorityLostError,
                            )

                            raise PublicAuthorityLostError(
                                PUBLIC_PERMISSION_LOST_MESSAGE
                            )
                        await _call_with_optional_conn(
                            verify_public_authority, effective_owner, conn=cur_conn
                        )
                    elif is_dalton_local_auth_enabled():
                        if not is_company_system_user(effective_owner):
                            eff_key = str(effective_owner).strip()
                            eff_tier = (
                                caller_tier
                                if eff_key == str(caller_id).strip()
                                else live_tiers.get(eff_key)
                                or await _resolve_live_tier_in_cur(cur, eff_key)
                            )
                            if eff_tier != "admin":
                                from src.dalton.monitors.service import (
                                    PUBLIC_PERMISSION_LOST_MESSAGE,
                                    PublicAuthorityLostError,
                                )

                                raise PublicAuthorityLostError(
                                    PUBLIC_PERMISSION_LOST_MESSAGE
                                )
                    else:
                        await _call_with_optional_conn(
                            verify_public_authority, effective_owner, conn=cur_conn
                        )

                acl_changed = (
                    cur_visibility != next_visibility
                    or (
                        next_visibility == "members"
                        and old_members != set(effective_members)
                    )
                    or (next_visibility != "members" and bool(old_members))
                )
                await cur.execute(
                    """
                    UPDATE dalton_monitors
                       SET title = %s,
                           category = %s,
                           sort_order = %s,
                           visibility = %s,
                           keep_updated = %s,
                           owner_user_id = %s,
                           acl_revision = acl_revision + %s,
                           updated_at = NOW()
                     WHERE monitor_id = %s::uuid
                    """,
                    (
                        next_title,
                        next_category,
                        next_sort,
                        next_visibility,
                        next_keep,
                        effective_owner,
                        1 if acl_changed else 0,
                        monitor_id,
                    ),
                )
                await cur.execute(
                    "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                    (monitor_id,),
                )
                if next_visibility == "members" and effective_members:
                    await cur.executemany(
                        """
                        INSERT INTO dalton_monitor_members (monitor_id, user_id)
                        VALUES (%s::uuid, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [(monitor_id, mid) for mid in effective_members],
                    )
                if was_public and next_visibility != "public":
                    await artifacts_records_db.unshare_in_cur(
                        cur, user_id=owner_user_id, artifact_id=artifact_id
                    )
                elif not was_public and next_visibility == "public" and published_seq > 0:
                    await _share_existing_version_publicly_in_cur(
                        cur, artifact_id, published_seq
                    )
                    await _record_monitor_version_in_cur(cur, monitor_id, published_seq)
                loaded = await _select_monitor_in_cur(cur, monitor_id)
    if loaded is None:
        raise LookupError(f"Monitor {monitor_id} not found")
    return loaded


def _signature_accepts_conn(fn: Any) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "conn" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _accepts_conn_kwarg(fn: Any) -> bool:
    side_effect = getattr(fn, "side_effect", None)
    if callable(side_effect):
        return _signature_accepts_conn(side_effect)
    return _signature_accepts_conn(fn)


async def _call_with_optional_conn(
    fn: Callable[..., Awaitable[Any]], arg: str, *, conn: Any
) -> Any:
    if conn is not None and _accepts_conn_kwarg(fn):
        return await fn(arg, conn=conn)
    return await fn(arg)


async def _resolve_live_tier_in_cur(cur: Any, user_id: str) -> str:
    """Resolve user tier inside an active transaction cursor (`FOR SHARE` on `dalton_user_roles` in `local` mode)."""
    from collections.abc import Mapping

    from src.dalton.monitors import permissions as monitor_perms
    from src.dalton.settings import is_dalton_local_auth_enabled

    uid = str(user_id or "").strip()
    if not uid:
        return "external"
    if is_company_system_user(uid):
        return "company"
    if is_dalton_local_auth_enabled():
        await cur.execute(
            "SELECT tier FROM dalton_user_roles WHERE user_id = %s FOR SHARE",
            (uid,),
        )
        row = await cur.fetchone()
        if row:
            raw_tier = row["tier"] if isinstance(row, Mapping) else row[0]
            tier = str(raw_tier or "").strip().lower()
            if tier in ("admin", "internal", "external"):
                return tier
        return "external"
    return await _call_with_optional_conn(
        monitor_perms.get_user_tier, uid, conn=getattr(cur, "connection", None)
    )


async def commit_monitor_publish_tx(
    *,
    slug: str,
    artifact_id: str,
    workspace_owner_id: str,
    monitor_owner_id: str,
    title: str,
    category: MonitorCategory,
    sort_order: int,
    visibility: MonitorVisibility,
    keep_updated: bool,
    source_kind: MonitorSourceKind,
    source_ref: str | None,
    member_user_ids: Sequence[str],
    files: list[VersionFile],
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
    reject_if_pending_outbound_confirm: bool = False,
) -> tuple[MonitorRecord, PublishResult]:
    """Upsert monitor metadata/members, verify public admin inside lock, reconcile share_links, cut, and record version."""
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

                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id, visibility, acl_revision
                      FROM dalton_monitors
                     WHERE artifact_id = %s::uuid
                       FOR UPDATE
                    """,
                    (artifact_id,),
                )
                existing = await cur.fetchone()
                if source_kind == "workspace":
                    await _reject_if_superseded_by_shared_document_in_cur(
                        cur, artifact_id, entry_path=getattr(locked, "path", None)
                    )
                if is_company_system_user(monitor_owner_id) or source_kind == "data_job":
                    if share_public and verify_public_authority is not None:
                        await _call_with_optional_conn(
                            verify_public_authority,
                            monitor_owner_id,
                            conn=getattr(cur, "connection", None),
                        )
                else:
                    from src.dalton.auth_guards import raise_external_member_forbidden
                    from src.dalton.monitors.permissions import (
                        EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE,
                        require_monitor_publish_visibility,
                    )
                    from src.dalton.settings import is_dalton_local_auth_enabled

                    live_tier = await _resolve_live_tier_in_cur(cur, monitor_owner_id)
                    if (
                        existing is not None
                        and live_tier == "external"
                        and existing["visibility"] != "own"
                    ):
                        raise_external_member_forbidden(
                            EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE
                        )
                    if live_tier == "external" and visibility != "own":
                        require_monitor_publish_visibility(live_tier, visibility)
                    if share_public and verify_public_authority is not None:
                        if is_dalton_local_auth_enabled():
                            if live_tier != "admin":
                                from src.dalton.monitors.service import (
                                    PUBLIC_PERMISSION_LOST_MESSAGE,
                                    PublicAuthorityLostError,
                                )

                                raise PublicAuthorityLostError(
                                    PUBLIC_PERMISSION_LOST_MESSAGE
                                )
                        else:
                            await _call_with_optional_conn(
                                verify_public_authority,
                                monitor_owner_id,
                                conn=getattr(cur, "connection", None),
                            )
                    require_monitor_publish_visibility(live_tier, visibility)

                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO dalton_monitors (
                            slug, artifact_id, owner_user_id, title, category,
                            sort_order, visibility, keep_updated, source_kind, source_ref
                        )
                        VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING monitor_id::text AS monitor_id
                        """,
                        (
                            slug,
                            artifact_id,
                            monitor_owner_id,
                            title,
                            category,
                            sort_order,
                            visibility,
                            keep_updated,
                            source_kind,
                            source_ref,
                        ),
                    )
                    inserted = await cur.fetchone()
                    monitor_id = str(inserted["monitor_id"])
                else:
                    monitor_id = str(existing["monitor_id"])
                    await cur.execute(
                        "SELECT user_id FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                        (monitor_id,),
                    )
                    old_members = {str(r["user_id"]) for r in await cur.fetchall()}
                    acl_changed = (
                        existing["visibility"] != visibility
                        or (visibility == "members" and old_members != set(clean_members))
                        or (visibility != "members" and bool(old_members))
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_monitors
                           SET owner_user_id = %s,
                               title = %s,
                               category = %s,
                               sort_order = %s,
                               visibility = %s,
                               keep_updated = %s,
                               source_kind = %s,
                               source_ref = %s,
                               acl_revision = acl_revision + %s,
                               updated_at = NOW()
                         WHERE monitor_id = %s::uuid
                        """,
                        (
                            monitor_owner_id,
                            title,
                            category,
                            sort_order,
                            visibility,
                            keep_updated,
                            source_kind,
                            source_ref,
                            1 if acl_changed else 0,
                            monitor_id,
                        ),
                    )

                await cur.execute(
                    "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
                    (monitor_id,),
                )
                if visibility == "members" and clean_members:
                    await cur.executemany(
                        """
                        INSERT INTO dalton_monitor_members (monitor_id, user_id)
                        VALUES (%s::uuid, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        [(monitor_id, mid) for mid in clean_members],
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
                await _record_monitor_version_in_cur(cur, monitor_id, result.seq)
                await _prune_monitor_versions_in_cur(
                    cur,
                    artifact_id,
                    published_seq=result.seq,
                    keep_count=MAX_MONITOR_VERSIONS,
                )
                await cur.execute(
                    """
                    UPDATE dalton_monitors
                       SET published_seq = %s,
                           last_verified_seq = %s,
                           last_verified_at = NOW(),
                           last_publish_error = NULL,
                           last_publish_failed_at = NULL,
                           updated_at = NOW()
                     WHERE monitor_id = %s::uuid
                    """,
                    (result.seq, result.seq, monitor_id),
                )
                loaded = await _select_monitor_in_cur(cur, monitor_id)
    if loaded is None:
        raise RuntimeError(f"Monitor {monitor_id} disappeared after commit")
    return loaded, result


async def _reserve_monitor_publish_run_in_cur(
    cur, *, monitor_id: str, run_id: str | None
) -> MonitorRecord | None:
    """Reserve ``(monitor_id, run_id)`` or return the existing monitor on duplicate run."""
    if run_id is None:
        return None
    await cur.execute(
        """
        INSERT INTO dalton_monitor_publish_runs (
            monitor_id, run_id, published_seq
        )
        VALUES (%s::uuid, %s, 0)
        ON CONFLICT (monitor_id, run_id) DO NOTHING
        RETURNING monitor_id::text AS monitor_id
        """,
        (monitor_id, run_id),
    )
    if await cur.fetchone() is None:
        existing_mon = await _select_monitor_in_cur(cur, monitor_id)
        if existing_mon is None:
            raise LookupError(f"Monitor {monitor_id} not found")
        return existing_mon
    return None


async def _cut_and_finalize_monitor_in_cur(
    cur,
    *,
    monitor_id: str,
    locked: Any,
    row: dict[str, Any],
    workspace_owner_id: str,
    files: list[VersionFile],
    title: str | None,
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    run_id: str | None = None,
    new_owner_user_id: str | None = None,
    caller_user_id: str | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[MonitorRecord, PublishResult]:
    """Verify authority, unshare if downgraded, cut artifact version, and update monitor state."""
    artifact_id = locked.artifact_id
    live_visibility: MonitorVisibility = row["visibility"]
    locked_owner = str(row["owner_user_id"])
    locked_source_kind = str(row.get("source_kind") or "workspace")
    share_public = live_visibility == "public"
    effective_owner = locked_owner

    cur_conn = getattr(cur, "connection", None)
    if locked_source_kind == "data_job" or (
        is_company_system_user(new_owner_user_id or locked_owner)
        and caller_user_id is None
    ):
        effective_owner = new_owner_user_id or locked_owner
        if share_public and verify_public_authority is not None:
            await _call_with_optional_conn(
                verify_public_authority, effective_owner, conn=cur_conn
            )
    else:
        from src.dalton.auth_guards import raise_external_member_forbidden
        from src.dalton.monitors.permissions import (
            can_manage_monitor_fields,
            require_monitor_publish_visibility,
        )
        from src.dalton.settings import is_dalton_local_auth_enabled

        # Lock involved role rows in deterministic ascending user_id order to match set_user_role.
        uids_to_lock = sorted(
            {
                str(u).strip()
                for u in (caller_user_id, new_owner_user_id, locked_owner)
                if u and str(u).strip()
            }
        )
        live_tiers: dict[str, str] = {}
        for uid in uids_to_lock:
            live_tiers[uid] = await _resolve_live_tier_in_cur(cur, uid)

        if caller_user_id is not None:
            caller_uid = str(caller_user_id)
            caller_tier = live_tiers[caller_uid]
            if not can_manage_monitor_fields(
                owner_user_id=locked_owner,
                visibility=live_visibility,
                caller_id=caller_uid,
                caller_tier=caller_tier,
            ):
                raise_external_member_forbidden("无权重新发布此监控面板")
            if new_owner_user_id is not None and str(new_owner_user_id) != locked_owner:
                if not (
                    share_public
                    and locked_source_kind == "workspace"
                    and caller_tier == "admin"
                ):
                    raise_external_member_forbidden("无权重新发布此监控面板")
                effective_owner = str(new_owner_user_id)
            if caller_tier == "external" and live_visibility != "own":
                require_monitor_publish_visibility(
                    caller_tier,
                    live_visibility,
                )
            if share_public and verify_public_authority is not None:
                if is_dalton_local_auth_enabled():
                    if caller_tier != "admin":
                        from src.dalton.monitors.service import (
                            PUBLIC_PERMISSION_LOST_MESSAGE,
                            PublicAuthorityLostError,
                        )

                        raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
                else:
                    await _call_with_optional_conn(
                        verify_public_authority, caller_uid, conn=cur_conn
                    )
            require_monitor_publish_visibility(
                caller_tier,
                live_visibility,
            )
        elif new_owner_user_id is not None and str(new_owner_user_id) != locked_owner:
            raise_external_member_forbidden("无权重新发布此监控面板")

        if not is_company_system_user(effective_owner) and (
            caller_user_id is None or str(effective_owner) != str(caller_user_id)
        ):
            live_tier = live_tiers[str(effective_owner)]
            if live_tier == "external" and live_visibility != "own":
                require_monitor_publish_visibility(
                    live_tier,
                    live_visibility,
                )
            if share_public and verify_public_authority is not None:
                if is_dalton_local_auth_enabled():
                    if live_tier != "admin":
                        from src.dalton.monitors.service import (
                            PUBLIC_PERMISSION_LOST_MESSAGE,
                            PublicAuthorityLostError,
                        )

                        raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
                else:
                    await _call_with_optional_conn(
                        verify_public_authority, effective_owner, conn=cur_conn
                    )
            require_monitor_publish_visibility(
                live_tier,
                live_visibility,
            )

    if read_basis is not None:
        read_basis.check(locked)

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
        label=label,
        share_public=share_public,
        origin=origin,
        loaded_files=loaded_files,
        left_out_files=left_out_files,
    )
    await _record_monitor_version_in_cur(cur, monitor_id, result.seq)
    await _prune_monitor_versions_in_cur(
        cur,
        artifact_id,
        published_seq=result.seq,
        keep_count=MAX_MONITOR_VERSIONS,
    )
    if run_id is not None:
        await cur.execute(
            """
            UPDATE dalton_monitor_publish_runs
               SET published_seq = %s
             WHERE monitor_id = %s::uuid AND run_id = %s
            """,
            (result.seq, monitor_id, run_id),
        )
    await cur.execute(
        """
        UPDATE dalton_monitors
           SET published_seq = %s,
               last_verified_seq = %s,
               last_verified_at = NOW(),
               owner_user_id = %s,
               last_publish_run_id = COALESCE(%s, last_publish_run_id),
               last_publish_error = NULL,
               last_publish_failed_at = NULL,
               updated_at = NOW()
         WHERE monitor_id = %s::uuid
        """,
        (result.seq, result.seq, effective_owner, run_id, monitor_id),
    )
    loaded = await _select_monitor_in_cur(cur, monitor_id)
    if loaded is None:
        raise LookupError(f"Monitor {monitor_id} not found")
    return loaded, result


async def commit_monitor_version_tx(
    *,
    monitor_id: str,
    artifact_id: str,
    workspace_owner_id: str,
    files: list[VersionFile],
    title: str | None,
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    run_id: str | None = None,
    new_owner_user_id: str | None = None,
    caller_user_id: str | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
    check_pending_outbound_confirm: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
) -> tuple[MonitorRecord, PublishResult | None, bool]:
    """Lock ``share_links`` + ``dalton_monitors``, re-read visibility, dedup ``run_id``, and cut atomically."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id,
                           visibility,
                           owner_user_id,
                           source_kind,
                           title,
                           published_seq
                      FROM dalton_monitors
                     WHERE monitor_id = %s::uuid
                       FOR UPDATE
                    """,
                    (monitor_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise LookupError(f"Monitor {monitor_id} not found")
                if row.get("source_kind", "workspace") == "workspace":
                    await _reject_if_superseded_by_shared_document_in_cur(
                        cur, artifact_id, entry_path=getattr(locked, "path", None)
                    )

                if check_pending_outbound_confirm or reject_if_pending_outbound_confirm:
                    from src.dalton.settings import is_dalton_sharing_auth_enabled

                    if is_dalton_sharing_auth_enabled():
                        from src.dalton.colleague_outbound_guard import (
                            WorkspaceAutoPublishPaused,
                            is_workspace_pending_outbound_confirm_in_cur,
                        )

                        if await is_workspace_pending_outbound_confirm_in_cur(
                            cur, locked.workspace_id
                        ):
                            if reject_if_pending_outbound_confirm:
                                raise WorkspaceAutoPublishPaused(locked.workspace_id)
                            existing = await _select_monitor_in_cur(cur, monitor_id)
                            if existing is None:
                                raise LookupError(f"Monitor {monitor_id} not found")
                            return existing, None, False

                dedup_mon = await _reserve_monitor_publish_run_in_cur(
                    cur, monitor_id=monitor_id, run_id=run_id
                )
                if dedup_mon is not None:
                    return dedup_mon, None, True

                loaded, result = await _cut_and_finalize_monitor_in_cur(
                    cur,
                    monitor_id=monitor_id,
                    locked=locked,
                    row=row,
                    workspace_owner_id=workspace_owner_id,
                    files=files,
                    title=title,
                    label=label,
                    read_basis=read_basis,
                    origin=origin,
                    loaded_files=loaded_files,
                    left_out_files=left_out_files,
                    run_id=run_id,
                    new_owner_user_id=new_owner_user_id,
                    caller_user_id=caller_user_id,
                    verify_public_authority=verify_public_authority,
                )
    return loaded, result, False


async def migrate_data_job_monitor_and_cut_tx(
    *,
    monitor_id: str,
    artifact_id: str,
    workspace_id: str,
    workspace_owner_id: str,
    target_entry_path: str,
    legacy_entry_path: str,
    expected_slug: str,
    target_source_ref: str,
    files: list[VersionFile],
    title: str | None,
    label: str | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    run_id: str | None = None,
    verify_public_authority: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[MonitorRecord, PublishResult | None, bool]:
    """Atomically rename a legacy ``data_job`` artifact path to ``<slug>/<entry_file>`` and cut."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur,
                    user_id=workspace_owner_id,
                    artifact_id=artifact_id,
                )
                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id,
                           slug,
                           artifact_id::text AS artifact_id,
                           source_kind,
                           source_ref,
                           visibility,
                           owner_user_id,
                           title,
                           published_seq
                      FROM dalton_monitors
                     WHERE monitor_id = %s::uuid
                       FOR UPDATE
                    """,
                    (monitor_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise LookupError(f"Monitor {monitor_id} not found")

                if (
                    row["source_kind"] != "data_job"
                    or not is_company_system_user(str(row["owner_user_id"]))
                    or row["slug"] != expected_slug
                    or str(row["artifact_id"]) != locked.artifact_id
                    or locked.workspace_id != workspace_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=f"监控面板 '{expected_slug}' 不符合遗留数据任务面板自动迁移条件",
                    )

                dedup_mon = await _reserve_monitor_publish_run_in_cur(
                    cur, monitor_id=monitor_id, run_id=run_id
                )
                if dedup_mon is not None:
                    return dedup_mon, None, True

                current_entry_path = locked.path
                if current_entry_path != target_entry_path:
                    if current_entry_path != legacy_entry_path:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"监控面板 '{expected_slug}' 仍绑定路径 '{current_entry_path}'，"
                                f"无法自动迁移到 '{target_entry_path}'"
                            ),
                        )
                    try:
                        locked = await artifacts_records_db.rename_artifact_path_in_cur(
                            cur,
                            user_id=workspace_owner_id,
                            artifact_id=locked.artifact_id,
                            workspace_id=locked.workspace_id,
                            new_path=target_entry_path,
                        )
                    except artifacts_records_db.ArtifactPathConflict as exc:
                        occupant_hint = (
                            f"（artifact_id={exc.occupant_artifact_id}）"
                            if exc.occupant_artifact_id
                            else ""
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"目标资源路径 '{target_entry_path}' 已被另一资源占用"
                                f"{occupant_hint}，无法自动迁移"
                            ),
                        ) from None

                if row.get("source_ref") != target_source_ref:
                    await cur.execute(
                        """
                        UPDATE dalton_monitors
                           SET source_ref = %s,
                               updated_at = NOW()
                         WHERE monitor_id = %s::uuid
                        """,
                        (target_source_ref, monitor_id),
                    )

                loaded, result = await _cut_and_finalize_monitor_in_cur(
                    cur,
                    monitor_id=monitor_id,
                    locked=locked,
                    row=row,
                    workspace_owner_id=workspace_owner_id,
                    files=files,
                    title=title,
                    label=label,
                    read_basis=None,
                    origin=origin,
                    loaded_files=loaded_files,
                    left_out_files=left_out_files,
                    run_id=run_id,
                    verify_public_authority=verify_public_authority,
                )
    return loaded, result, False


def _clip_publish_error(message: str | None, limit: int = 1000) -> str:
    """Clip a monitor publish error to ``limit`` chars while preserving trailing ``| 数据日期: ...``."""
    raw = (message or "Republish failed").strip()
    if len(raw) <= limit:
        return raw
    marker = " | 数据日期: "
    idx = raw.rfind(marker)
    if idx != -1:
        suffix = raw[idx:]
        if len(suffix) < limit - 20:
            body_budget = limit - len(suffix) - 1
            return raw[:body_budget].rstrip() + "…" + suffix
    return raw[:limit]


async def record_monitor_publish_failure(
    monitor_id: str,
    *,
    error_message: str,
    run_id: str | None = None,
    expected_claim_epoch: int | None = None,
) -> MonitorRecord | None:
    """Record the latest failure message/timestamp without marking ``run_id`` as processed.

    When both ``run_id`` and ``expected_claim_epoch`` are supplied, the write is fenced
    against ``dalton_data_task_runs`` so a late/stale worker cannot overwrite
    ``last_publish_error`` after another worker has taken over or settled the run.
    Skips updating legacy workspace monitors already taken over by ``dalton_shared_documents``.
    """
    clipped = _clip_publish_error(error_message, limit=1000)
    if run_id is not None and expected_claim_epoch is not None:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT monitor_publish_status,
                               monitor_publish_claim_epoch
                          FROM dalton_data_task_runs
                         WHERE run_id = %s::uuid
                           FOR SHARE
                        """,
                        (run_id,),
                    )
                    run_row = await cur.fetchone()
                    if (
                        run_row is not None
                        and str(run_row.get("monitor_publish_status") or "") == "publishing"
                        and int(run_row.get("monitor_publish_claim_epoch") or 0)
                        == int(expected_claim_epoch)
                    ):
                        await cur.execute(
                            """
                            UPDATE dalton_monitors
                               SET last_publish_error = %s,
                                   last_publish_failed_at = NOW()
                             WHERE monitor_id = %s::uuid
                               AND NOT (
                                   source_kind = 'workspace'
                                   AND EXISTS (
                                       SELECT 1
                                         FROM dalton_shared_documents d
                                        WHERE d.artifact_id = dalton_monitors.artifact_id
                                   )
                               )
                            """,
                            (clipped, monitor_id),
                        )
        return await get_monitor_by_id(monitor_id)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_monitors
                   SET last_publish_error = %s,
                       last_publish_failed_at = NOW()
                 WHERE monitor_id = %s::uuid
                   AND NOT (
                       source_kind = 'workspace'
                       AND EXISTS (
                           SELECT 1
                             FROM dalton_shared_documents d
                            WHERE d.artifact_id = dalton_monitors.artifact_id
                       )
                   )
                """,
                (clipped, monitor_id),
            )
    return await get_monitor_by_id(monitor_id)


async def delete_monitor_tx(
    monitor_id: str,
    *,
    artifact_id: str,
    workspace_owner_id: str,
    caller_id: str,
    resolve_caller_tier: Callable[[str], Awaitable[str]] | None = None,
) -> bool:
    """Lock ``share_links`` and ``dalton_monitors``, re-verify management authority in lock, unshare if public/shared, and delete."""
    tier_lookup = resolve_caller_tier or get_user_tier
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                await cur.execute(
                    """
                    SELECT visibility, owner_user_id, source_kind
                      FROM dalton_monitors
                     WHERE monitor_id = %s::uuid
                       FOR UPDATE
                    """,
                    (monitor_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                if row.get("source_kind", "workspace") == "workspace":
                    await _reject_if_superseded_by_shared_document_in_cur(
                        cur, artifact_id, entry_path=getattr(locked, "path", None)
                    )
                from src.dalton.settings import is_dalton_local_auth_enabled

                if (
                    resolve_caller_tier is None or resolve_caller_tier is get_user_tier
                ) and is_dalton_local_auth_enabled():
                    caller_tier = await _resolve_live_tier_in_cur(cur, caller_id)
                else:
                    caller_tier = await _call_with_optional_conn(
                        tier_lookup, caller_id, conn=getattr(cur, "connection", None)
                    )
                if not can_manage_monitor_fields(
                    owner_user_id=str(row["owner_user_id"]),
                    visibility=row["visibility"],
                    caller_id=caller_id,
                    caller_tier=caller_tier,
                ):
                    raise HTTPException(status_code=403, detail="无权删除此监控面板")
                if row["visibility"] == "public" or locked.shared:
                    await artifacts_records_db.unshare_in_cur(
                        cur, user_id=workspace_owner_id, artifact_id=artifact_id
                    )
                await cur.execute(
                    "DELETE FROM dalton_monitors WHERE monitor_id = %s::uuid",
                    (monitor_id,),
                )
                return (cur.rowcount or 0) > 0


_UNSET: Any = object()


@dataclass(frozen=True)
class StagedMonitorCandidate:
    """Result of Stage 1 staging: candidate files stored in ``artifact_versions`` without moving visitor pointers."""

    artifact_id: str
    candidate_seq: int
    cut: bool
    deduped: bool = False
    paused: bool = False
    existing_monitor: MonitorRecord | None = None
    share_revision: int | None = None
    share_audience: str | None = None
    monitor_acl_revision: int | None = None
    left_out_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotedMonitorResult:
    """Result of Stage 3 atomic pointer promotion."""

    monitor: MonitorRecord | None
    promoted: bool
    deduped: bool = False
    superseded_by_newer: bool = False
    fenced_out: bool = False
    deferred_to_newer: bool = False


@dataclass(frozen=True)
class PromoteMonitorItem:
    """Per-panel parameters for single-transaction multi-panel ``all_or_nothing`` promotion."""

    artifact_id: str
    candidate_seq: int
    workspace_owner_id: str
    paths: Sequence[str]
    monitor_id: str | None = None
    slug: str | None = None
    monitor_owner_id: str | None = None
    title: str | None = None
    category: MonitorCategory | None = None
    sort_order: int | None = None
    visibility: MonitorVisibility | None = None
    keep_updated: bool | None = None
    source_kind: MonitorSourceKind = "data_job"
    source_ref: str | None = None
    member_user_ids: Sequence[str] | None = None
    new_owner_user_id: str | None = None
    caller_user_id: str | None = None
    verify_public_authority: Callable[..., Awaitable[None]] | None = None
    check_pending_outbound_confirm: bool = False
    reject_if_pending_outbound_confirm: bool = False
    update_metadata: bool = False
    is_update_flow: bool = False
    expected_share_revision: int | None = None
    expected_share_audience: Any = _UNSET
    expected_acl_revision: int | None = None
    expected_left_out_files: Collection[str] | None = None
    migrate_entry_path: str | None = None
    migrate_legacy_entry_path: str | None = None
    migrate_source_ref: str | None = None
    migrate_workspace_id: str | None = None


@dataclass(frozen=True)
class BatchPromoteResult:
    """Result of atomic single-transaction multi-panel promotion."""

    results: list[PromotedMonitorResult]
    promoted: bool
    superseded_by_newer: bool = False
    fenced_out: bool = False
    deferred_to_newer: bool = False


class _BatchPromoteSuperseded(Exception):
    """Internal rollback sentinel when any panel in an ``all_or_nothing`` batch is superseded."""


_FAILED_OR_CANCELLED_RUN_STATUSES = frozenset(
    {
        "failed",
        "timed_out",
        "aborted",
        "skipped_low_memory",
        "skipped_superseded",
        "cancelled",
    }
)
_ACTIVE_RUN_STATUSES = frozenset({"queued", "starting", "running"})
_TERMINAL_SUPERSEDING_MONITOR_PUBLISH_STATUSES = frozenset(
    {"published", "unchanged", "partial_failed"}
)
_IN_FLIGHT_MONITOR_PUBLISH_STATUSES = frozenset({"pending", "retrying", "publishing"})
_FAILED_MONITOR_PUBLISH_STATUSES = frozenset({"failed", "disabled"})


def _classify_newer_data_job_run(row: Mapping[str, Any]) -> str:
    """Classify a newer ``dalton_data_task_runs`` row for cross-run publish fencing.

    Returns:
    - ``'superseded'``: newer run succeeded and reached a terminal publish state
      (``published`` / ``unchanged`` / ``partial_failed``), so older runs are permanently superseded.
    - ``'deferred'``: newer run is still active (``queued`` / ``starting`` / ``running``) or
      succeeded with publish still in-flight (``pending`` / ``retrying`` / ``publishing``), so
      older runs must not advance pointers yet but must remain reclaimable if the newer run fails.
    - ``'none'``: newer run failed, was cancelled, or failed/disabled monitor publishing and does
      not block older runs.
    """
    r_status = str(row.get("status") or "")
    r_mp_status = str(row.get("monitor_publish_status") or "")
    if r_status in _FAILED_OR_CANCELLED_RUN_STATUSES:
        return "none"
    if r_status in _ACTIVE_RUN_STATUSES:
        return "deferred"
    if r_status == "succeeded":
        if r_mp_status in _TERMINAL_SUPERSEDING_MONITOR_PUBLISH_STATUSES:
            return "superseded"
        if r_mp_status in _IN_FLIGHT_MONITOR_PUBLISH_STATUSES:
            return "deferred"
        return "none"
    return "none"


def _is_blocking_newer_data_job_run(row: Mapping[str, Any]) -> bool:
    """Return True if a newer run row either defers or terminally supersedes an older run."""
    return _classify_newer_data_job_run(row) != "none"


async def _check_data_job_run_fences_in_cur(
    cur: Any,
    *,
    run_id: str | None,
    expected_claim_epoch: int | None,
    check_superseded: bool,
) -> str:
    """Verify ``claim_epoch`` and in-transaction newer-run fences on ``dalton_data_task_runs``.

    Returns ``'ok'``, ``'fenced_out'``, ``'superseded'``, or ``'deferred'``.
    """
    if run_id is None or (expected_claim_epoch is None and not check_superseded):
        return "ok"
    await cur.execute(
        """
        SELECT run_id::text AS run_id,
               job_key,
               status,
               scheduled_slot_utc,
               completed_at,
               monitor_publish_status,
               monitor_publish_claim_epoch
          FROM dalton_data_task_runs
         WHERE run_id = %s::uuid
           FOR UPDATE
        """,
        (run_id,),
    )
    run_row = await cur.fetchone()
    if expected_claim_epoch is not None:
        if (
            run_row is None
            or str(run_row.get("monitor_publish_status") or "") != "publishing"
            or int(run_row.get("monitor_publish_claim_epoch") or 0) != int(expected_claim_epoch)
        ):
            return "fenced_out"
    if check_superseded and run_row is not None and str(run_row.get("status") or "") == "succeeded":
        # Lock all newer non-failed/non-cancelled runs (including queued/starting/running
        # and succeeded runs) FOR SHARE (ordered by run_id to avoid deadlocks) AFTER holding
        # the artifact lock so an older replay never advances pointers ahead of a newer run.
        await cur.execute(
            """
            SELECT run_id::text AS run_id,
                   status,
                   monitor_publish_status
              FROM dalton_data_task_runs
             WHERE job_key = %(job_key)s
               AND run_id <> %(run_id)s::uuid
               AND status NOT IN (
                   'failed',
                   'timed_out',
                   'aborted',
                   'skipped_low_memory',
                   'skipped_superseded',
                   'cancelled'
               )
               AND (
                   (%(slot_utc)s::timestamptz IS NOT NULL AND scheduled_slot_utc > %(slot_utc)s::timestamptz)
                   OR (
                       (%(slot_utc)s::timestamptz IS NULL OR scheduled_slot_utc = %(slot_utc)s::timestamptz)
                       AND COALESCE(completed_at, updated_at) > COALESCE(%(completed_at)s::timestamptz, '-infinity'::timestamptz)
                   )
               )
             ORDER BY run_id ASC
               FOR SHARE
            """,
            {
                "job_key": str(run_row["job_key"]),
                "run_id": str(run_row["run_id"]),
                "slot_utc": run_row.get("scheduled_slot_utc"),
                "completed_at": run_row.get("completed_at"),
            },
        )
        newer_rows = await cur.fetchall()
        classifications = [_classify_newer_data_job_run(r) for r in (newer_rows or ())]
        if "superseded" in classifications:
            return "superseded"
        if "deferred" in classifications:
            return "deferred"
    return "ok"


async def _verify_stage_or_promote_authority_in_cur(
    cur: Any,
    *,
    existing: dict[str, Any] | None,
    visibility: MonitorVisibility,
    monitor_owner_id: str,
    source_kind: MonitorSourceKind,
    caller_user_id: str | None = None,
    new_owner_user_id: str | None = None,
    verify_public_authority: Callable[..., Awaitable[None]] | None = None,
    is_update_flow: bool = False,
) -> str:
    """Re-verify publish/update authority under row locks and return ``effective_owner``."""
    cur_conn = getattr(cur, "connection", None)
    share_public = visibility == "public"
    locked_owner = str(existing["owner_user_id"]) if existing is not None else str(monitor_owner_id)
    effective_owner = locked_owner if is_update_flow else str(monitor_owner_id)

    if source_kind == "data_job" or (
        is_company_system_user(new_owner_user_id or effective_owner)
        and caller_user_id is None
    ):
        effective_owner = new_owner_user_id or effective_owner
        if share_public and verify_public_authority is not None:
            await _call_with_optional_conn(
                verify_public_authority, effective_owner, conn=cur_conn
            )
        return effective_owner

    from src.dalton.auth_guards import raise_external_member_forbidden
    from src.dalton.monitors.permissions import (
        EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE,
        can_manage_monitor_fields,
        require_monitor_publish_visibility,
    )
    from src.dalton.settings import is_dalton_local_auth_enabled

    if not is_update_flow:
        live_tier = await _resolve_live_tier_in_cur(cur, monitor_owner_id)
        if (
            existing is not None
            and live_tier == "external"
            and existing["visibility"] != "own"
        ):
            raise_external_member_forbidden(EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE)
        if live_tier == "external" and visibility != "own":
            require_monitor_publish_visibility(live_tier, visibility)
        if share_public and verify_public_authority is not None:
            if is_dalton_local_auth_enabled():
                if live_tier != "admin":
                    from src.dalton.monitors.service import (
                        PUBLIC_PERMISSION_LOST_MESSAGE,
                        PublicAuthorityLostError,
                    )

                    raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
            else:
                await _call_with_optional_conn(
                    verify_public_authority, monitor_owner_id, conn=cur_conn
                )
        require_monitor_publish_visibility(live_tier, visibility)
        return str(monitor_owner_id)

    uids_to_lock = sorted(
        {
            str(u).strip()
            for u in (caller_user_id, new_owner_user_id, locked_owner)
            if u and str(u).strip()
        }
    )
    live_tiers: dict[str, str] = {}
    for uid in uids_to_lock:
        live_tiers[uid] = await _resolve_live_tier_in_cur(cur, uid)

    if caller_user_id is not None:
        caller_uid = str(caller_user_id)
        caller_tier = live_tiers[caller_uid]
        if not can_manage_monitor_fields(
            owner_user_id=locked_owner,
            visibility=visibility,
            caller_id=caller_uid,
            caller_tier=caller_tier,
        ):
            raise_external_member_forbidden("无权重新发布此监控面板")
        if new_owner_user_id is not None and str(new_owner_user_id) != locked_owner:
            if not (share_public and source_kind == "workspace" and caller_tier == "admin"):
                raise_external_member_forbidden("无权重新发布此监控面板")
            effective_owner = str(new_owner_user_id)
        if caller_tier == "external" and visibility != "own":
            require_monitor_publish_visibility(caller_tier, visibility)
        if share_public and verify_public_authority is not None:
            if is_dalton_local_auth_enabled():
                if caller_tier != "admin":
                    from src.dalton.monitors.service import (
                        PUBLIC_PERMISSION_LOST_MESSAGE,
                        PublicAuthorityLostError,
                    )

                    raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
            else:
                await _call_with_optional_conn(
                    verify_public_authority, caller_uid, conn=cur_conn
                )
        require_monitor_publish_visibility(caller_tier, visibility)
    elif new_owner_user_id is not None and str(new_owner_user_id) != locked_owner:
        raise_external_member_forbidden("无权重新发布此监控面板")

    if not is_company_system_user(effective_owner) and (
        caller_user_id is None or str(effective_owner) != str(caller_user_id)
    ):
        live_tier = live_tiers[str(effective_owner)]
        if live_tier == "external" and visibility != "own":
            require_monitor_publish_visibility(live_tier, visibility)
        if share_public and verify_public_authority is not None:
            if is_dalton_local_auth_enabled():
                if live_tier != "admin":
                    from src.dalton.monitors.service import (
                        PUBLIC_PERMISSION_LOST_MESSAGE,
                        PublicAuthorityLostError,
                    )

                    raise PublicAuthorityLostError(PUBLIC_PERMISSION_LOST_MESSAGE)
            else:
                await _call_with_optional_conn(
                    verify_public_authority, effective_owner, conn=cur_conn
                )
        require_monitor_publish_visibility(live_tier, visibility)

    return effective_owner


async def stage_monitor_candidate_version_tx(
    *,
    artifact_id: str,
    workspace_owner_id: str,
    files: list[VersionFile],
    title: str | None,
    label: str | None = None,
    read_basis: ReadBasis | None = None,
    origin: Origin = "owner",
    loaded_files: list[str] | None = None,
    left_out_files: list[str] | None = None,
    monitor_id: str | None = None,
    monitor_owner_id: str | None = None,
    visibility: MonitorVisibility | None = None,
    source_kind: MonitorSourceKind = "workspace",
    caller_user_id: str | None = None,
    new_owner_user_id: str | None = None,
    verify_public_authority: Callable[..., Awaitable[None]] | None = None,
    check_pending_outbound_confirm: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
    run_id: str | None = None,
    is_update_flow: bool = False,
    candidate_entry_path: str | None = None,
    expected_legacy_entry_path: str | None = None,
    expected_slug: str | None = None,
    expected_workspace_id: str | None = None,
) -> StagedMonitorCandidate:
    """Stage 1: write candidate rows to ``artifact_versions`` / ``artifact_version_files`` only.

    Leaves ``share_links.shared_seq``, ``share_links.files``, ``share_links.path``,
    ``dalton_monitors.published_seq``, ``dalton_monitors.last_verified_seq``,
    ``dalton_monitor_versions``, and ``dalton_monitor_publish_runs`` completely untouched.
    On first-time publish, no row is created in ``dalton_monitors`` yet so a failed
    verification leaves zero shell rows.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                if read_basis is not None:
                    read_basis.check(locked)

                existing_row: dict[str, Any] | None = None
                if monitor_id is not None:
                    await cur.execute(
                        """
                        SELECT monitor_id::text AS monitor_id,
                               slug,
                               artifact_id::text AS artifact_id,
                               visibility,
                               owner_user_id,
                               source_kind,
                               source_ref,
                               title,
                               published_seq,
                               last_verified_seq,
                               acl_revision
                          FROM dalton_monitors
                         WHERE monitor_id = %s::uuid
                           FOR UPDATE
                        """,
                        (monitor_id,),
                    )
                    existing_row = await cur.fetchone()
                    if existing_row is None:
                        raise LookupError(f"Monitor {monitor_id} not found")
                else:
                    await cur.execute(
                        """
                        SELECT monitor_id::text AS monitor_id,
                               slug,
                               artifact_id::text AS artifact_id,
                               visibility,
                               owner_user_id,
                               source_kind,
                               source_ref,
                               title,
                               published_seq,
                               last_verified_seq,
                               acl_revision
                          FROM dalton_monitors
                         WHERE artifact_id = %s::uuid
                           FOR UPDATE
                        """,
                        (artifact_id,),
                    )
                    existing_row = await cur.fetchone()

                # Same takeover guard as commit_monitor_version_tx (D4 shared documents).
                if existing_row is not None and existing_row.get("source_kind", "workspace") == "workspace":
                    await _reject_if_superseded_by_shared_document_in_cur(
                        cur, artifact_id, entry_path=candidate_entry_path or getattr(locked, "path", None)
                    )

                if check_pending_outbound_confirm or reject_if_pending_outbound_confirm:
                    from src.dalton.settings import is_dalton_sharing_auth_enabled

                    if is_dalton_sharing_auth_enabled():
                        from src.dalton.colleague_outbound_guard import (
                            WorkspaceAutoPublishPaused,
                            is_workspace_pending_outbound_confirm_in_cur,
                        )

                        if await is_workspace_pending_outbound_confirm_in_cur(
                            cur, locked.workspace_id
                        ):
                            if reject_if_pending_outbound_confirm:
                                raise WorkspaceAutoPublishPaused(locked.workspace_id)
                            existing_mon = (
                                await _select_monitor_in_cur(cur, str(existing_row["monitor_id"]))
                                if existing_row is not None
                                else None
                            )
                            return StagedMonitorCandidate(
                                artifact_id=artifact_id,
                                candidate_seq=int(locked.latest_seq or 0),
                                cut=False,
                                paused=True,
                                existing_monitor=existing_mon,
                            )

                if existing_row is not None and run_id is not None:
                    await cur.execute(
                        """
                        SELECT 1 FROM dalton_monitor_publish_runs
                         WHERE monitor_id = %s::uuid AND run_id = %s
                        """,
                        (existing_row["monitor_id"], run_id),
                    )
                    if await cur.fetchone() is not None:
                        existing_mon = await _select_monitor_in_cur(
                            cur, str(existing_row["monitor_id"])
                        )
                        return StagedMonitorCandidate(
                            artifact_id=artifact_id,
                            candidate_seq=int(
                                existing_mon.published_seq
                                if existing_mon is not None
                                else (existing_row.get("published_seq") or 0)
                            ),
                            cut=False,
                            deduped=True,
                            existing_monitor=existing_mon,
                        )

                eff_visibility: MonitorVisibility = (
                    visibility
                    if visibility is not None
                    else (existing_row["visibility"] if existing_row is not None else "own")
                )
                eff_source_kind: MonitorSourceKind = (
                    existing_row["source_kind"]
                    if existing_row is not None and is_update_flow
                    else source_kind
                )
                eff_owner_arg = (
                    monitor_owner_id
                    if monitor_owner_id is not None
                    else (
                        str(existing_row["owner_user_id"])
                        if existing_row is not None
                        else workspace_owner_id
                    )
                )
                await _verify_stage_or_promote_authority_in_cur(
                    cur,
                    existing=existing_row,
                    visibility=eff_visibility,
                    monitor_owner_id=eff_owner_arg,
                    source_kind=eff_source_kind,
                    caller_user_id=caller_user_id,
                    new_owner_user_id=new_owner_user_id,
                    verify_public_authority=verify_public_authority,
                    is_update_flow=is_update_flow,
                )

                stage_target = locked
                if candidate_entry_path is not None and locked.path != candidate_entry_path:
                    if (
                        existing_row is None
                        or existing_row["source_kind"] != "data_job"
                        or not is_company_system_user(str(existing_row["owner_user_id"]))
                        or (expected_slug is not None and existing_row.get("slug") != expected_slug)
                        or str(existing_row.get("artifact_id") or "") != locked.artifact_id
                        or (
                            expected_workspace_id is not None
                            and locked.workspace_id != expected_workspace_id
                        )
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=f"监控面板 '{expected_slug or artifact_id}' 不符合遗留数据任务面板自动迁移条件",
                        )
                    if (
                        expected_legacy_entry_path is not None
                        and locked.path != expected_legacy_entry_path
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"监控面板 '{expected_slug or artifact_id}' 仍绑定路径 '{locked.path}'，"
                                f"无法自动迁移到 '{candidate_entry_path}'"
                            ),
                        )
                    # Fail fast before staging candidate rows if target path is already occupied.
                    await cur.execute(
                        """
                        SELECT artifact_id::text AS artifact_id
                          FROM share_links
                         WHERE workspace_id = %s::uuid
                           AND kind = 'file'
                           AND path = %s
                           AND artifact_id <> %s::uuid
                        """,
                        (locked.workspace_id, candidate_entry_path, locked.artifact_id),
                    )
                    occupant = await cur.fetchone()
                    if occupant is not None:
                        occupant_aid = str(occupant.get("artifact_id") or "")
                        occupant_hint = f"（artifact_id={occupant_aid}）" if occupant_aid else ""
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"目标资源路径 '{candidate_entry_path}' 已被另一资源占用"
                                f"{occupant_hint}，无法自动迁移"
                            ),
                        )
                    # Stage candidate version under target entry_path while keeping share_links.path untouched until Stage 3.
                    stage_target = replace(locked, path=candidate_entry_path)

                result = await artifacts_versions_db.cut_monitor_in_cur(
                    cur,
                    stage_target,
                    user_id=workspace_owner_id,
                    files=files,
                    title=title or (str(existing_row["title"]) if existing_row else None),
                    label=label,
                    share_public=False,
                    origin=origin,
                    loaded_files=loaded_files,
                    left_out_files=left_out_files,
                )
                existing_mon = (
                    await _select_monitor_in_cur(cur, str(existing_row["monitor_id"]))
                    if existing_row is not None
                    else None
                )
                staged_share_rev = int(result.artifact.revision)
                staged_share_audience = result.artifact.audience
                staged_left_out = tuple(sorted(result.artifact.left_out_files))
                staged_acl_rev = (
                    int(existing_row["acl_revision"])
                    if existing_row is not None and existing_row.get("acl_revision") is not None
                    else None
                )
    return StagedMonitorCandidate(
        artifact_id=artifact_id,
        candidate_seq=result.seq,
        cut=result.cut,
        deduped=False,
        paused=False,
        existing_monitor=existing_mon,
        share_revision=staged_share_rev,
        share_audience=staged_share_audience,
        monitor_acl_revision=staged_acl_rev,
        left_out_files=staged_left_out,
    )


async def _promote_verified_monitor_in_cur(
    cur: Any,
    *,
    artifact_id: str,
    candidate_seq: int,
    workspace_owner_id: str,
    paths: Sequence[str],
    locked_artifact: Any = None,
    monitor_id: str | None = None,
    slug: str | None = None,
    monitor_owner_id: str | None = None,
    title: str | None = None,
    category: MonitorCategory | None = None,
    sort_order: int | None = None,
    visibility: MonitorVisibility | None = None,
    keep_updated: bool | None = None,
    source_kind: MonitorSourceKind = "workspace",
    source_ref: str | None = None,
    member_user_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    new_owner_user_id: str | None = None,
    caller_user_id: str | None = None,
    verify_public_authority: Callable[..., Awaitable[None]] | None = None,
    check_pending_outbound_confirm: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
    update_metadata: bool = False,
    is_update_flow: bool = False,
    expected_share_revision: int | None = None,
    expected_share_audience: Any = _UNSET,
    expected_acl_revision: int | None = None,
    expected_left_out_files: Collection[str] | None = None,
    migrate_entry_path: str | None = None,
    migrate_legacy_entry_path: str | None = None,
    migrate_source_ref: str | None = None,
    migrate_workspace_id: str | None = None,
) -> PromotedMonitorResult:
    """Promote one verified monitor candidate inside an active transaction cursor."""
    sorted_paths = sorted(dict.fromkeys(str(p) for p in paths if str(p)))
    clean_members: list[str] | None = (
        list(dict.fromkeys(str(m).strip() for m in member_user_ids if str(m).strip()))
        if member_user_ids is not None
        else None
    )
    locked = (
        locked_artifact
        if locked_artifact is not None
        else await artifacts_records_db.lock_owned(
            cur, user_id=workspace_owner_id, artifact_id=artifact_id
        )
    )

    # Fence 2a: share_links revision, audience & left_out_files must match what was staged.
    if expected_share_revision is not None and int(locked.revision) != int(expected_share_revision):
        raise SharingChanged(
            f"share_links revision moved during verification ({expected_share_revision} -> {locked.revision})"
        )
    if expected_share_audience is not _UNSET and locked.audience != expected_share_audience:
        raise SharingChanged(
            f"share_links audience changed during verification ({expected_share_audience!r} -> {locked.audience!r})"
        )
    locked_left_out = frozenset(getattr(locked, "left_out_files", ()) or ())
    if expected_left_out_files is not None and locked_left_out != frozenset(expected_left_out_files):
        raise LeftOutChanged(
            f"share_links left_out_files changed during verification "
            f"({sorted(expected_left_out_files)!r} -> {sorted(locked_left_out)!r})"
        )
    excluded_overlap = sorted(set(sorted_paths) & locked_left_out)
    if excluded_overlap:
        raise LeftOutChanged(
            f"Candidate v{candidate_seq} includes files excluded in left_out_files: {excluded_overlap}"
        )

    if check_pending_outbound_confirm or reject_if_pending_outbound_confirm:
        from src.dalton.settings import is_dalton_sharing_auth_enabled

        if is_dalton_sharing_auth_enabled():
            from src.dalton.colleague_outbound_guard import (
                WorkspaceAutoPublishPaused,
                is_workspace_pending_outbound_confirm_in_cur,
            )

            if await is_workspace_pending_outbound_confirm_in_cur(
                cur, locked.workspace_id
            ):
                if reject_if_pending_outbound_confirm:
                    raise WorkspaceAutoPublishPaused(locked.workspace_id)
                if monitor_id is not None:
                    paused_mon = await _select_monitor_in_cur(cur, monitor_id)
                    if paused_mon is not None:
                        return PromotedMonitorResult(
                            monitor=paused_mon,
                            promoted=False,
                            deduped=False,
                        )
                raise WorkspaceAutoPublishPaused(locked.workspace_id)

    if monitor_id is not None:
        await cur.execute(
            """
            SELECT monitor_id::text AS monitor_id,
                   slug,
                   artifact_id::text AS artifact_id,
                   visibility,
                   owner_user_id,
                   source_kind,
                   source_ref,
                   title,
                   published_seq,
                   last_verified_seq,
                   acl_revision
              FROM dalton_monitors
             WHERE monitor_id = %s::uuid
               FOR UPDATE
            """,
            (monitor_id,),
        )
    else:
        await cur.execute(
            """
            SELECT monitor_id::text AS monitor_id,
                   slug,
                   artifact_id::text AS artifact_id,
                   visibility,
                   owner_user_id,
                   source_kind,
                   source_ref,
                   title,
                   published_seq,
                   last_verified_seq,
                   acl_revision
              FROM dalton_monitors
             WHERE artifact_id = %s::uuid
               FOR UPDATE
            """,
            (artifact_id,),
        )
    existing = await cur.fetchone()

    # Fence 2b: monitor ACL revision must match what was staged (detects concurrent unshare / audience change).
    if expected_acl_revision is not None:
        if existing is None or int(existing["acl_revision"]) != int(expected_acl_revision):
            raise SharingChanged(
                f"monitor acl_revision changed during verification ({expected_acl_revision} -> "
                f"{existing['acl_revision'] if existing is not None else None})"
            )

    if existing is None:
        if not slug or title is None or category is None or visibility is None:
            raise ValueError("First-time monitor promotion requires slug, title, category, and visibility")
        eff_owner = await _verify_stage_or_promote_authority_in_cur(
            cur,
            existing=None,
            visibility=visibility,
            monitor_owner_id=monitor_owner_id or workspace_owner_id,
            source_kind=source_kind,
            caller_user_id=caller_user_id,
            new_owner_user_id=new_owner_user_id,
            verify_public_authority=verify_public_authority,
            is_update_flow=False,
        )
        await cur.execute(
            """
            INSERT INTO dalton_monitors (
                slug, artifact_id, owner_user_id, title, category,
                sort_order, visibility, published_seq,
                last_verified_seq, last_verified_at,
                keep_updated, source_kind, source_ref, last_publish_run_id
            )
            VALUES (
                %s, %s::uuid, %s, %s, %s,
                %s, %s, %s,
                %s, NOW(),
                %s, %s, %s, %s
            )
            RETURNING monitor_id::text AS monitor_id
            """,
            (
                slug,
                artifact_id,
                eff_owner,
                title,
                category,
                int(sort_order or 0),
                visibility,
                candidate_seq,
                candidate_seq,
                bool(keep_updated),
                source_kind,
                source_ref,
                run_id,
            ),
        )
        inserted = await cur.fetchone()
        resolved_monitor_id = str(inserted["monitor_id"])
        if visibility == "members" and clean_members:
            await cur.executemany(
                """
                INSERT INTO dalton_monitor_members (monitor_id, user_id)
                VALUES (%s::uuid, %s)
                ON CONFLICT DO NOTHING
                """,
                [(resolved_monitor_id, mid) for mid in clean_members],
            )
        await _record_monitor_version_in_cur(cur, resolved_monitor_id, candidate_seq)
        if run_id is not None:
            await cur.execute(
                """
                INSERT INTO dalton_monitor_publish_runs (monitor_id, run_id, published_seq)
                VALUES (%s::uuid, %s, %s)
                ON CONFLICT (monitor_id, run_id) DO NOTHING
                """,
                (resolved_monitor_id, run_id, candidate_seq),
            )
        if visibility == "public":
            await _sync_share_link_promoted_in_cur(
                cur,
                artifact_id=artifact_id,
                seq=candidate_seq,
                sorted_paths=sorted_paths,
                share_public=True,
            )
        elif locked.shared:
            await artifacts_records_db.unshare_in_cur(
                cur, user_id=workspace_owner_id, artifact_id=artifact_id
            )
        await _prune_monitor_versions_in_cur(
            cur,
            artifact_id,
            published_seq=candidate_seq,
            keep_count=MAX_MONITOR_VERSIONS,
        )
        loaded = await _select_monitor_in_cur(cur, resolved_monitor_id)
        if loaded is None:
            raise RuntimeError(f"Monitor {resolved_monitor_id} disappeared after promote")
        return PromotedMonitorResult(monitor=loaded, promoted=True)

    # Existing monitor row locked FOR UPDATE
    resolved_monitor_id = str(existing["monitor_id"])
    dedup_mon = await _reserve_monitor_publish_run_in_cur(
        cur, monitor_id=resolved_monitor_id, run_id=run_id
    )
    if dedup_mon is not None:
        return PromotedMonitorResult(
            monitor=dedup_mon, promoted=False, deduped=True
        )

    eff_visibility: MonitorVisibility = (
        visibility if (update_metadata and visibility is not None) else existing["visibility"]
    )
    eff_source_kind: MonitorSourceKind = (
        source_kind if update_metadata else existing["source_kind"]
    )
    eff_owner = await _verify_stage_or_promote_authority_in_cur(
        cur,
        existing=existing,
        visibility=eff_visibility,
        monitor_owner_id=monitor_owner_id or str(existing["owner_user_id"]),
        source_kind=eff_source_kind,
        caller_user_id=caller_user_id,
        new_owner_user_id=new_owner_user_id,
        verify_public_authority=verify_public_authority,
        is_update_flow=is_update_flow and not update_metadata,
    )

    current_published_seq = max(
        int(existing["published_seq"] or 0),
        int(existing.get("last_verified_seq") or 0),
    )
    if candidate_seq < current_published_seq:
        # Stale verification completed after a newer version already promoted.
        if run_id is not None:
            await cur.execute(
                """
                DELETE FROM dalton_monitor_publish_runs
                 WHERE monitor_id = %s::uuid AND run_id = %s AND published_seq = 0
                """,
                (resolved_monitor_id, run_id),
            )
        loaded = await _select_monitor_in_cur(cur, resolved_monitor_id)
        if loaded is None:
            raise LookupError(f"Monitor {resolved_monitor_id} not found")
        return PromotedMonitorResult(
            monitor=loaded,
            promoted=False,
            deduped=False,
            superseded_by_newer=True,
        )

    if migrate_entry_path is not None and locked.path != migrate_entry_path:
        if (
            existing["source_kind"] != "data_job"
            or not is_company_system_user(str(existing["owner_user_id"]))
            or (slug is not None and existing.get("slug") != slug)
            or str(existing.get("artifact_id") or "") != locked.artifact_id
            or (migrate_workspace_id is not None and locked.workspace_id != migrate_workspace_id)
        ):
            raise HTTPException(
                status_code=409,
                detail=f"监控面板 '{slug or resolved_monitor_id}' 不符合遗留数据任务面板自动迁移条件",
            )
        if migrate_legacy_entry_path is not None and locked.path != migrate_legacy_entry_path:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"监控面板 '{slug or resolved_monitor_id}' 仍绑定路径 '{locked.path}'，"
                    f"无法自动迁移到 '{migrate_entry_path}'"
                ),
            )
        try:
            locked = await artifacts_records_db.rename_artifact_path_in_cur(
                cur,
                user_id=workspace_owner_id,
                artifact_id=locked.artifact_id,
                workspace_id=locked.workspace_id,
                new_path=migrate_entry_path,
            )
        except artifacts_records_db.ArtifactPathConflict as exc:
            occupant_hint = (
                f"（artifact_id={exc.occupant_artifact_id}）"
                if exc.occupant_artifact_id
                else ""
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"目标资源路径 '{migrate_entry_path}' 已被另一资源占用"
                    f"{occupant_hint}，无法自动迁移"
                ),
            ) from None
        if migrate_source_ref is not None and existing.get("source_ref") != migrate_source_ref:
            await cur.execute(
                """
                UPDATE dalton_monitors
                   SET source_ref = %s,
                       updated_at = NOW()
                 WHERE monitor_id = %s::uuid
                """,
                (migrate_source_ref, resolved_monitor_id),
            )

    if update_metadata:
        await cur.execute(
            "SELECT user_id FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
            (resolved_monitor_id,),
        )
        old_members = {str(r["user_id"]) for r in await cur.fetchall()}
        next_members = clean_members if clean_members is not None else sorted(old_members)
        acl_changed = (
            existing["visibility"] != eff_visibility
            or (eff_visibility == "members" and old_members != set(next_members))
            or (eff_visibility != "members" and bool(old_members))
        )
        await cur.execute(
            """
            UPDATE dalton_monitors
               SET owner_user_id = %s,
                   title = %s,
                   category = %s,
                   sort_order = %s,
                   visibility = %s,
                   keep_updated = %s,
                   source_kind = %s,
                   source_ref = %s,
                   acl_revision = acl_revision + %s,
                   updated_at = NOW()
             WHERE monitor_id = %s::uuid
            """,
            (
                eff_owner,
                title or str(existing["title"]),
                category or "other",
                int(sort_order or 0),
                eff_visibility,
                bool(keep_updated),
                eff_source_kind,
                source_ref,
                1 if acl_changed else 0,
                resolved_monitor_id,
            ),
        )
        await cur.execute(
            "DELETE FROM dalton_monitor_members WHERE monitor_id = %s::uuid",
            (resolved_monitor_id,),
        )
        if eff_visibility == "members" and next_members:
            await cur.executemany(
                """
                INSERT INTO dalton_monitor_members (monitor_id, user_id)
                VALUES (%s::uuid, %s)
                ON CONFLICT DO NOTHING
                """,
                [(resolved_monitor_id, mid) for mid in next_members],
            )

    if candidate_seq > current_published_seq:
        await cur.execute(
            """
            UPDATE dalton_monitors
               SET published_seq = %s,
                   last_verified_seq = %s,
                   last_verified_at = NOW(),
                   owner_user_id = %s,
                   last_publish_run_id = COALESCE(%s, last_publish_run_id),
                   last_publish_error = NULL,
                   last_publish_failed_at = NULL,
                   updated_at = NOW()
             WHERE monitor_id = %s::uuid
               AND published_seq < %s
            """,
            (
                candidate_seq,
                candidate_seq,
                eff_owner,
                run_id,
                resolved_monitor_id,
                candidate_seq,
            ),
        )
        if cur.rowcount != 1:
            loaded = await _select_monitor_in_cur(cur, resolved_monitor_id)
            if loaded is None:
                raise LookupError(f"Monitor {resolved_monitor_id} not found")
            return PromotedMonitorResult(
                monitor=loaded,
                promoted=False,
                superseded_by_newer=True,
            )
    else:
        # Unchanged seq (candidate_seq == current_published_seq): refresh verified timestamp and clear error.
        await cur.execute(
            """
            UPDATE dalton_monitors
               SET last_verified_seq = %s,
                   last_verified_at = NOW(),
                   owner_user_id = %s,
                   last_publish_run_id = COALESCE(%s, last_publish_run_id),
                   last_publish_error = NULL,
                   last_publish_failed_at = NULL,
                   updated_at = NOW()
             WHERE monitor_id = %s::uuid
            """,
            (candidate_seq, eff_owner, run_id, resolved_monitor_id),
        )

    await _record_monitor_version_in_cur(cur, resolved_monitor_id, candidate_seq)
    if run_id is not None:
        await cur.execute(
            """
            UPDATE dalton_monitor_publish_runs
               SET published_seq = %s
             WHERE monitor_id = %s::uuid AND run_id = %s
            """,
            (candidate_seq, resolved_monitor_id, run_id),
        )
    if eff_visibility == "public":
        await _sync_share_link_promoted_in_cur(
            cur,
            artifact_id=artifact_id,
            seq=candidate_seq,
            sorted_paths=sorted_paths,
            share_public=True,
        )
    elif locked.shared:
        await artifacts_records_db.unshare_in_cur(
            cur, user_id=workspace_owner_id, artifact_id=artifact_id
        )
    await _prune_monitor_versions_in_cur(
        cur,
        artifact_id,
        published_seq=candidate_seq,
        keep_count=MAX_MONITOR_VERSIONS,
    )
    loaded = await _select_monitor_in_cur(cur, resolved_monitor_id)
    if loaded is None:
        raise LookupError(f"Monitor {resolved_monitor_id} not found")
    return PromotedMonitorResult(monitor=loaded, promoted=True)


async def promote_verified_monitor_version_tx(
    *,
    artifact_id: str,
    candidate_seq: int,
    workspace_owner_id: str,
    paths: Sequence[str],
    monitor_id: str | None = None,
    slug: str | None = None,
    monitor_owner_id: str | None = None,
    title: str | None = None,
    category: MonitorCategory | None = None,
    sort_order: int | None = None,
    visibility: MonitorVisibility | None = None,
    keep_updated: bool | None = None,
    source_kind: MonitorSourceKind = "workspace",
    source_ref: str | None = None,
    member_user_ids: Sequence[str] | None = None,
    run_id: str | None = None,
    new_owner_user_id: str | None = None,
    caller_user_id: str | None = None,
    verify_public_authority: Callable[..., Awaitable[None]] | None = None,
    check_pending_outbound_confirm: bool = False,
    reject_if_pending_outbound_confirm: bool = False,
    update_metadata: bool = False,
    is_update_flow: bool = False,
    expected_claim_epoch: int | None = None,
    check_superseded: bool = False,
    expected_share_revision: int | None = None,
    expected_share_audience: Any = _UNSET,
    expected_acl_revision: int | None = None,
    expected_left_out_files: Collection[str] | None = None,
    migrate_entry_path: str | None = None,
    migrate_legacy_entry_path: str | None = None,
    migrate_source_ref: str | None = None,
    migrate_workspace_id: str | None = None,
) -> PromotedMonitorResult:
    """Stage 3: atomically promote a verified candidate version under all fences AFTER holding the artifact lock:

    0. Acquire ``share_links`` row lock (``lock_owned``) first to serialize concurrent promotes;
    1. ``claim_epoch`` + in-tx ``superseded`` (``FOR SHARE`` on newer runs) fence on ``dalton_data_task_runs``;
    2. ``share_links.revision`` / ``audience`` / ``left_out_files`` / ``dalton_monitors.acl_revision`` fence;
    3. Optional atomic legacy ``index.html`` -> ``<slug>/index.html`` path rename + pointer advance.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Acquire artifact lock FIRST so cross-run supersede & left_out checks run under the lock.
                locked = await artifacts_records_db.lock_owned(
                    cur, user_id=workspace_owner_id, artifact_id=artifact_id
                )
                fence = await _check_data_job_run_fences_in_cur(
                    cur,
                    run_id=run_id,
                    expected_claim_epoch=expected_claim_epoch,
                    check_superseded=check_superseded,
                )
                if fence != "ok":
                    existing_mon = (
                        await _select_monitor_in_cur(cur, monitor_id)
                        if monitor_id is not None
                        else None
                    )
                    return PromotedMonitorResult(
                        monitor=existing_mon,
                        promoted=False,
                        superseded_by_newer=(fence == "superseded"),
                        fenced_out=(fence == "fenced_out"),
                        deferred_to_newer=(fence == "deferred"),
                    )
                return await _promote_verified_monitor_in_cur(
                    cur,
                    artifact_id=artifact_id,
                    candidate_seq=candidate_seq,
                    workspace_owner_id=workspace_owner_id,
                    paths=paths,
                    locked_artifact=locked,
                    monitor_id=monitor_id,
                    slug=slug,
                    monitor_owner_id=monitor_owner_id,
                    title=title,
                    category=category,
                    sort_order=sort_order,
                    visibility=visibility,
                    keep_updated=keep_updated,
                    source_kind=source_kind,
                    source_ref=source_ref,
                    member_user_ids=member_user_ids,
                    run_id=run_id,
                    new_owner_user_id=new_owner_user_id,
                    caller_user_id=caller_user_id,
                    verify_public_authority=verify_public_authority,
                    check_pending_outbound_confirm=check_pending_outbound_confirm,
                    reject_if_pending_outbound_confirm=reject_if_pending_outbound_confirm,
                    update_metadata=update_metadata,
                    is_update_flow=is_update_flow,
                    expected_share_revision=expected_share_revision,
                    expected_share_audience=expected_share_audience,
                    expected_acl_revision=expected_acl_revision,
                    expected_left_out_files=expected_left_out_files,
                    migrate_entry_path=migrate_entry_path,
                    migrate_legacy_entry_path=migrate_legacy_entry_path,
                    migrate_source_ref=migrate_source_ref,
                    migrate_workspace_id=migrate_workspace_id,
                )


async def promote_verified_monitor_batch_tx(
    items: Sequence[PromoteMonitorItem],
    *,
    run_id: str | None = None,
    expected_claim_epoch: int | None = None,
    check_superseded: bool = False,
) -> BatchPromoteResult:
    """Atomically promote ALL verified panels for an ``all_or_nothing`` batch in ONE transaction.

    Acquires all ``share_links`` artifact locks in sorted ``artifact_id`` order BEFORE checking
    ``claim_epoch`` / ``superseded`` / ``left_out_files``. If any fence fails or any panel
    fails/is superseded, the entire transaction rolls back so zero visitor pointers move.
    """
    if not items:
        return BatchPromoteResult(results=[], promoted=True)

    indexed = sorted(enumerate(items), key=lambda pair: (pair[1].artifact_id, pair[0]))
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    locked_by_idx: dict[int, Any] = {}
                    for orig_idx, item in indexed:
                        locked_by_idx[orig_idx] = await artifacts_records_db.lock_owned(
                            cur,
                            user_id=item.workspace_owner_id,
                            artifact_id=item.artifact_id,
                        )
                    fence = await _check_data_job_run_fences_in_cur(
                        cur,
                        run_id=run_id,
                        expected_claim_epoch=expected_claim_epoch,
                        check_superseded=check_superseded,
                    )
                    if fence != "ok":
                        return BatchPromoteResult(
                            results=[],
                            promoted=False,
                            superseded_by_newer=(fence == "superseded"),
                            fenced_out=(fence == "fenced_out"),
                            deferred_to_newer=(fence == "deferred"),
                        )

                    ordered: list[PromotedMonitorResult | None] = [None] * len(items)
                    for orig_idx, item in indexed:
                        res = await _promote_verified_monitor_in_cur(
                            cur,
                            artifact_id=item.artifact_id,
                            candidate_seq=item.candidate_seq,
                            workspace_owner_id=item.workspace_owner_id,
                            paths=item.paths,
                            locked_artifact=locked_by_idx[orig_idx],
                            monitor_id=item.monitor_id,
                            slug=item.slug,
                            monitor_owner_id=item.monitor_owner_id,
                            title=item.title,
                            category=item.category,
                            sort_order=item.sort_order,
                            visibility=item.visibility,
                            keep_updated=item.keep_updated,
                            source_kind=item.source_kind,
                            source_ref=item.source_ref,
                            member_user_ids=item.member_user_ids,
                            run_id=run_id,
                            new_owner_user_id=item.new_owner_user_id,
                            caller_user_id=item.caller_user_id,
                            verify_public_authority=item.verify_public_authority,
                            check_pending_outbound_confirm=item.check_pending_outbound_confirm,
                            reject_if_pending_outbound_confirm=item.reject_if_pending_outbound_confirm,
                            update_metadata=item.update_metadata,
                            is_update_flow=item.is_update_flow,
                            expected_share_revision=item.expected_share_revision,
                            expected_share_audience=item.expected_share_audience,
                            expected_acl_revision=item.expected_acl_revision,
                            expected_left_out_files=item.expected_left_out_files,
                            migrate_entry_path=item.migrate_entry_path,
                            migrate_legacy_entry_path=item.migrate_legacy_entry_path,
                            migrate_source_ref=item.migrate_source_ref,
                            migrate_workspace_id=item.migrate_workspace_id,
                        )
                        if res.superseded_by_newer:
                            raise _BatchPromoteSuperseded()
                        ordered[orig_idx] = res
                    final_results = [r for r in ordered if r is not None]
                    return BatchPromoteResult(results=final_results, promoted=True)
    except _BatchPromoteSuperseded:
        return BatchPromoteResult(
            results=[],
            promoted=False,
            superseded_by_newer=True,
        )


async def _sync_share_link_promoted_in_cur(
    cur: Any,
    *,
    artifact_id: str,
    seq: int,
    sorted_paths: Sequence[str],
    share_public: bool,
) -> None:
    """Atomically advance ``share_links`` visitor fields (``shared_seq``, ``files``, ``shared_at``, ``revision``)."""
    sets = [
        "shared_seq = %(seq)s",
        "files = %(files)s::text[]",
        "legacy_refused = NULL",
    ]
    if share_public:
        sets += [
            "shared_at = COALESCE(l.shared_at, NOW())",
            "revision = l.revision + CASE WHEN l.shared_at IS NULL THEN 1 ELSE 0 END",
        ]
    await cur.execute(
        f"""
        UPDATE share_links l
           SET {", ".join(sets)}
         WHERE l.artifact_id = %(artifact_id)s::uuid
        """,
        {
            "seq": seq,
            "files": list(sorted_paths),
            "artifact_id": artifact_id,
        },
    )


async def _prune_monitor_versions_in_cur(
    cur,
    artifact_id: str,
    *,
    published_seq: int,
    keep_count: int = MAX_MONITOR_VERSIONS,
) -> int:
    await cur.execute(
        """
        DELETE FROM artifact_versions v
         WHERE v.artifact_id = %(artifact_id)s::uuid
           AND v.seq <> %(published_seq)s
           AND v.seq <> COALESCE(
               (SELECT GREATEST(COALESCE(last_verified_seq, 0), COALESCE(published_seq, 0))
                  FROM dalton_monitors
                 WHERE artifact_id = %(artifact_id)s::uuid),
               0
           )
           AND v.seq <> COALESCE(
               (SELECT latest_seq FROM share_links WHERE artifact_id = %(artifact_id)s::uuid),
               0
           )
           AND v.seq <> COALESCE(
               (SELECT shared_seq FROM share_links WHERE artifact_id = %(artifact_id)s::uuid),
               0
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM artifact_comments ac
                 JOIN artifact_version_files avf
                   ON avf.artifact_id = v.artifact_id
                  AND avf.seq = v.seq
                  AND avf.path = ac.path
                  AND avf.sha256 = ac.file_sha256
                WHERE ac.artifact_id = v.artifact_id
                  AND ac.file_sha256 IS NOT NULL
           )
           AND v.seq < (
               SELECT MIN(k.seq)
                 FROM (
                     SELECT seq
                       FROM artifact_versions
                      WHERE artifact_id = %(artifact_id)s::uuid
                      ORDER BY seq DESC
                      LIMIT %(keep_count)s
                 ) k
           )
        """,
        {
            "artifact_id": artifact_id,
            "published_seq": published_seq,
            "keep_count": keep_count,
        },
    )
    deleted_versions = int(cur.rowcount or 0)
    if deleted_versions > 0:
        await cur.execute(
            """
            DELETE FROM dalton_monitor_versions dmv
             USING dalton_monitors dm
             WHERE dmv.monitor_id = dm.monitor_id
               AND dm.artifact_id = %s::uuid
               AND NOT EXISTS (
                   SELECT 1
                     FROM artifact_versions av
                    WHERE av.artifact_id = dm.artifact_id
                      AND av.seq = dmv.seq
               )
            """,
            (artifact_id,),
        )
    return deleted_versions


def _normalize_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _row_to_monitor_data_entry(row: dict[str, Any]) -> MonitorDataEntry:
    return MonitorDataEntry(
        monitor_id=str(row["monitor_id"]),
        data_key=str(row["data_key"]),
        payload=row.get("payload"),
        as_of=row.get("as_of"),
        sha256=str(row["sha256"]) if row.get("sha256") is not None else None,
        updated_at=row["updated_at"],
        attempt_seq=int(row.get("attempt_seq") or 0),
        is_stale=bool(row.get("is_stale", False)),
        last_attempt_at=row.get("last_attempt_at"),
        last_error=row.get("last_error"),
    )


async def upsert_monitor_data(
    monitor_id: str,
    data_key: str,
    payload: Any = None,
    *,
    as_of: datetime | None = None,
    attempt_seq: int | None = None,
    is_stale: bool = False,
    error: str | None = None,
    last_attempt_at: datetime | None = None,
) -> MonitorDataEntry:
    """Dual-axis monitor data upsert: snapshot axis (``as_of``) + collector health axis (``attempt_seq``).

    - Snapshot axis (``payload``, ``as_of``, ``sha256``): evaluated only when ``is_stale`` is False
      and ``payload is not None``; advances on ``stored.as_of IS NULL`` or ``incoming.as_of > stored.as_of``,
      accepts idempotent equal ``as_of`` + ``sha256`` (or legacy wall-clock stale payload self-heal),
      and returns HTTP 409 on backwards ``as_of`` against a fresh snapshot.
    - Health axis (``attempt_seq``, ``is_stale``, ``last_attempt_at``, ``last_error``): advances
      strictly monotonically on ``incoming.attempt_seq > stored.attempt_seq``. Out-of-order late
      packets with smaller/equal ``attempt_seq`` never overwrite newer health state.
    - Failure reports (``is_stale=True``) leave existing snapshots intact and create a snapshot-less
      row (``payload=NULL, as_of=NULL, sha256=NULL``) on first boot.
    """
    if not is_stale and payload is None:
        raise HTTPException(
            status_code=400,
            detail="Fresh monitor data push requires non-null data payload",
        )

    attempt_ts = _normalize_utc(last_attempt_at) if last_attempt_at is not None else datetime.now(UTC)
    clean_error = (
        str(error).strip()[:1000]
        if error is not None and str(error).strip()
        else None
    )

    canonical: str | None = None
    digest: str | None = None
    ts: datetime | None = None
    if not is_stale and payload is not None:
        canonical = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        ts = _normalize_utc(as_of) if as_of is not None else datetime.now(UTC)

    legacy_payload_stale = (
        attempt_seq is None
        and not is_stale
        and isinstance(payload, dict)
        and payload.get("stale") is True
    )
    eff_initial_stale = bool(is_stale or legacy_payload_stale)
    initial_attempt_seq = int(attempt_seq) if attempt_seq is not None else 1

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id,
                           data_key,
                           payload,
                           as_of,
                           sha256,
                           updated_at,
                           attempt_seq,
                           is_stale,
                           last_attempt_at,
                           last_error
                      FROM dalton_monitor_data
                     WHERE monitor_id = %s::uuid AND data_key = %s
                       FOR UPDATE
                    """,
                    (monitor_id, data_key),
                )
                existing = await cur.fetchone()

                if existing is None:
                    await cur.execute(
                        """
                        INSERT INTO dalton_monitor_data (
                            monitor_id, data_key, payload, as_of, sha256, updated_at,
                            attempt_seq, is_stale, last_attempt_at, last_error
                        )
                        VALUES (%s::uuid, %s, %s::jsonb, %s, %s, NOW(), %s, %s, %s, %s)
                        ON CONFLICT (monitor_id, data_key) DO NOTHING
                        RETURNING monitor_id::text AS monitor_id,
                                  data_key,
                                  payload,
                                  as_of,
                                  sha256,
                                  updated_at,
                                  attempt_seq,
                                  is_stale,
                                  last_attempt_at,
                                  last_error
                        """,
                        (
                            monitor_id,
                            data_key,
                            canonical,
                            ts,
                            digest,
                            initial_attempt_seq,
                            eff_initial_stale,
                            attempt_ts,
                            clean_error if eff_initial_stale else None,
                        ),
                    )
                    inserted = await cur.fetchone()
                    if inserted is not None:
                        return _row_to_monitor_data_entry(inserted)
                    # Concurrent insert won the race; lock and continue to update path.
                    await cur.execute(
                        """
                        SELECT monitor_id::text AS monitor_id,
                               data_key,
                               payload,
                               as_of,
                               sha256,
                               updated_at,
                               attempt_seq,
                               is_stale,
                               last_attempt_at,
                               last_error
                          FROM dalton_monitor_data
                         WHERE monitor_id = %s::uuid AND data_key = %s
                           FOR UPDATE
                        """,
                        (monitor_id, data_key),
                    )
                    existing = await cur.fetchone()
                    if existing is None:
                        raise HTTPException(
                            status_code=500,
                            detail="Monitor data upsert produced no row",
                        )

                stored_payload = existing.get("payload")
                stored_as_of_raw = existing.get("as_of")
                stored_as_of = (
                    _normalize_utc(stored_as_of_raw)
                    if stored_as_of_raw is not None
                    else None
                )
                stored_sha = (
                    str(existing["sha256"])
                    if existing.get("sha256") is not None
                    else None
                )
                stored_attempt_seq = int(existing.get("attempt_seq") or 0)
                stored_was_legacy_stale = (
                    isinstance(stored_payload, dict)
                    and stored_payload.get("stale") is True
                )

                stored_is_stale = bool(existing.get("is_stale", False))
                is_identical_healthy_replay = (
                    attempt_seq is None
                    and not is_stale
                    and not stored_is_stale
                    and ts is not None
                    and stored_as_of is not None
                    and ts == stored_as_of
                    and digest is not None
                    and stored_sha == digest
                )
                incoming_seq = (
                    int(attempt_seq)
                    if attempt_seq is not None
                    else (stored_attempt_seq if is_identical_healthy_replay else stored_attempt_seq + 1)
                )
                advance_health = incoming_seq > stored_attempt_seq

                # Evaluate snapshot axis
                update_snapshot = False
                if not is_stale and ts is not None and digest is not None:
                    if stored_as_of is None or ts > stored_as_of:
                        update_snapshot = True
                    elif ts == stored_as_of:
                        if stored_sha == digest:
                            update_snapshot = False
                        elif stored_was_legacy_stale or (attempt_seq is not None and advance_health):
                            update_snapshot = True
                        else:
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Rejected stale monitor snapshot for {data_key}: "
                                    f"incoming as_of={ts.isoformat()} is not newer than "
                                    f"stored as_of={stored_as_of.isoformat()}"
                                ),
                            )
                    else:
                        # ts < stored_as_of: allow one-time self-heal if stored row was legacy wall-clock stale
                        if stored_was_legacy_stale:
                            update_snapshot = True
                        else:
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Rejected stale monitor snapshot for {data_key}: "
                                    f"incoming as_of={ts.isoformat()} is not newer than "
                                    f"stored as_of={stored_as_of.isoformat()}"
                                ),
                            )

                if not update_snapshot and not advance_health:
                    return _row_to_monitor_data_entry(existing)

                next_attempt_seq = incoming_seq if advance_health else stored_attempt_seq
                next_is_stale = (
                    eff_initial_stale
                    if advance_health
                    else bool(existing.get("is_stale", False))
                )
                next_last_attempt_at = (
                    attempt_ts if advance_health else existing.get("last_attempt_at")
                )
                next_last_error = (
                    (clean_error if next_is_stale else None)
                    if advance_health
                    else existing.get("last_error")
                )

                if update_snapshot:
                    await cur.execute(
                        """
                        UPDATE dalton_monitor_data
                           SET payload = %s::jsonb,
                               as_of = %s,
                               sha256 = %s,
                               attempt_seq = %s,
                               is_stale = %s,
                               last_attempt_at = %s,
                               last_error = %s,
                               updated_at = NOW()
                         WHERE monitor_id = %s::uuid AND data_key = %s
                        RETURNING monitor_id::text AS monitor_id,
                                  data_key,
                                  payload,
                                  as_of,
                                  sha256,
                                  updated_at,
                                  attempt_seq,
                                  is_stale,
                                  last_attempt_at,
                                  last_error
                        """,
                        (
                            canonical,
                            ts,
                            digest,
                            next_attempt_seq,
                            next_is_stale,
                            next_last_attempt_at,
                            next_last_error,
                            monitor_id,
                            data_key,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE dalton_monitor_data
                           SET attempt_seq = %s,
                               is_stale = %s,
                               last_attempt_at = %s,
                               last_error = %s,
                               updated_at = NOW()
                         WHERE monitor_id = %s::uuid AND data_key = %s
                        RETURNING monitor_id::text AS monitor_id,
                                  data_key,
                                  payload,
                                  as_of,
                                  sha256,
                                  updated_at,
                                  attempt_seq,
                                  is_stale,
                                  last_attempt_at,
                                  last_error
                        """,
                        (
                            next_attempt_seq,
                            next_is_stale,
                            next_last_attempt_at,
                            next_last_error,
                            monitor_id,
                            data_key,
                        ),
                    )
                updated = await cur.fetchone()
                if updated is None:
                    raise HTTPException(
                        status_code=500, detail="Monitor data update produced no row"
                    )
                return _row_to_monitor_data_entry(updated)


async def get_monitor_data(monitor_id: str, data_key: str) -> MonitorDataEntry | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT monitor_id::text AS monitor_id,
                       data_key,
                       payload,
                       as_of,
                       sha256,
                       updated_at,
                       attempt_seq,
                       is_stale,
                       last_attempt_at,
                       last_error
                  FROM dalton_monitor_data
                 WHERE monitor_id = %s::uuid AND data_key = %s
                """,
                (monitor_id, data_key),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_monitor_data_entry(row)


async def list_company_members() -> list[CompanyMember]:
    """Return company members (``admin`` or ``internal`` via ``access_tiers.get_user_tier``)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, email, name
                  FROM users
                 ORDER BY COALESCE(NULLIF(BTRIM(name), ''), email, user_id)
                """
            )
            rows = await cur.fetchall()

    members: list[CompanyMember] = []
    for row in rows:
        uid = str(row["user_id"])
        if is_company_system_user(uid):
            continue
        tier = await get_user_tier(uid)
        if tier in ("admin", "internal"):
            members.append(
                CompanyMember(
                    user_id=uid,
                    email=row.get("email"),
                    name=row.get("name"),
                    tier=tier,
                )
            )
    return members
