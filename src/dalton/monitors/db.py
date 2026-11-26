"""Database persistence for Dalton Monitors, members, data payloads, and version pruning."""

from __future__ import annotations

import hashlib
import inspect
import json
import posixpath
from collections.abc import Awaitable, Callable, Sequence
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
    is_valid_slug,
    slugify_candidate,
)
from src.dalton.monitors.permissions import can_manage_monitor_fields
from src.server.database.artifacts import records as artifacts_records_db
from src.server.database.artifacts import versions as artifacts_versions_db
from src.server.database.artifacts.versions import (
    Origin,
    PublishResult,
    ReadBasis,
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
                ORDER BY m.slug
                """,
                (workspace_id,),
            )
            return [r for row in await cur.fetchall() if (r := _row_to_record(row))]


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
                await artifacts_records_db.lock_owned(
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
                           last_publish_error = NULL,
                           last_publish_failed_at = NULL,
                           updated_at = NOW()
                     WHERE monitor_id = %s::uuid
                    """,
                    (result.seq, monitor_id),
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
               owner_user_id = %s,
               last_publish_run_id = COALESCE(%s, last_publish_run_id),
               last_publish_error = NULL,
               last_publish_failed_at = NULL,
               updated_at = NOW()
         WHERE monitor_id = %s::uuid
        """,
        (result.seq, effective_owner, run_id, monitor_id),
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
) -> MonitorRecord | None:
    """Record the latest failure message/timestamp without marking ``run_id`` as processed."""
    _ = run_id
    clipped = _clip_publish_error(error_message, limit=1000)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_monitors
                   SET last_publish_error = %s,
                       last_publish_failed_at = NOW()
                 WHERE monitor_id = %s::uuid
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
                    SELECT visibility, owner_user_id
                      FROM dalton_monitors
                     WHERE monitor_id = %s::uuid
                       FOR UPDATE
                    """,
                    (monitor_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
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


async def upsert_monitor_data(
    monitor_id: str,
    data_key: str,
    payload: Any,
    *,
    as_of: datetime | None = None,
) -> MonitorDataEntry:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ts = _normalize_utc(as_of) if as_of is not None else datetime.now(UTC)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_monitor_data (
                    monitor_id, data_key, payload, as_of, sha256, updated_at
                )
                VALUES (%s::uuid, %s, %s::jsonb, %s, %s, NOW())
                ON CONFLICT (monitor_id, data_key) DO UPDATE
                   SET payload = EXCLUDED.payload,
                       as_of = EXCLUDED.as_of,
                       sha256 = EXCLUDED.sha256,
                       updated_at = NOW()
                 WHERE EXCLUDED.as_of > dalton_monitor_data.as_of
                RETURNING monitor_id::text AS monitor_id, data_key, payload, as_of, sha256, updated_at
                """,
                (monitor_id, data_key, canonical, ts, digest),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """
                    SELECT monitor_id::text AS monitor_id, data_key, payload, as_of, sha256, updated_at
                      FROM dalton_monitor_data
                     WHERE monitor_id = %s::uuid AND data_key = %s
                    """,
                    (monitor_id, data_key),
                )
                existing = await cur.fetchone()
                if existing is None:
                    raise HTTPException(
                        status_code=500,
                        detail="Monitor data upsert produced no row",
                    )
                existing_as_of = _normalize_utc(existing["as_of"])
                existing_sha = str(existing["sha256"])
                if existing_as_of == ts and existing_sha == digest:
                    # Idempotent replay of the exact same snapshot: keep original updated_at untouched.
                    row = existing
                else:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Rejected stale monitor snapshot for {data_key}: "
                            f"incoming as_of={ts.isoformat()} is not newer than "
                            f"stored as_of={existing_as_of.isoformat()}"
                        ),
                    )
    return MonitorDataEntry(
        monitor_id=str(row["monitor_id"]),
        data_key=str(row["data_key"]),
        payload=row["payload"],
        as_of=row["as_of"],
        sha256=str(row["sha256"]),
        updated_at=row["updated_at"],
    )


async def get_monitor_data(monitor_id: str, data_key: str) -> MonitorDataEntry | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT monitor_id::text AS monitor_id, data_key, payload, as_of, sha256, updated_at
                  FROM dalton_monitor_data
                 WHERE monitor_id = %s::uuid AND data_key = %s
                """,
                (monitor_id, data_key),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return MonitorDataEntry(
        monitor_id=str(row["monitor_id"]),
        data_key=str(row["data_key"]),
        payload=row["payload"],
        as_of=row["as_of"],
        sha256=str(row["sha256"]),
        updated_at=row["updated_at"],
    )


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
