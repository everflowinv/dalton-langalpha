"""Post-turn direct-edit detection and next-turn file reconciliation (PK.5)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import publications as db_publications
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge.paths import (
    AGENT_MD,
    AGENT_SUMMARY_END,
    AGENT_SUMMARY_START_PREFIX,
    RENDERED,
    data_path,
    history_edited_md_path,
    history_path,
    rendered_path,
)
from src.dalton.knowledge.profiles import (
    PROFILE_COMPANY,
    PROFILE_SECTOR,
    PROFILE_SPECIFIC_DOCS,
    SHARED_DOC_KEYS,
    doc_keys_for_profile,
    doc_not_allowed_message,
    resolve_workspace_profile,
)
from src.dalton.settings import is_coverage_units_enabled
from src.dalton.knowledge.render import (
    format_alert_lines,
    render_agent_block,
    render_document_md,
    update_agent_md_block,
)
from src.dalton.knowledge.service import (
    canonical_json,
    write_version_files_to_backend,
)
from src.dalton.knowledge.validate import validate_update
from src.server.database.workspace_file import (
    get_file as db_get_file,
    get_file_metadata_for_sync,
)
from src.server.services.persistence.resolve import resolve_file_text_or_none

logger = logging.getLogger(__name__)


def same_content(a: Any, b: Any) -> bool:
    """Compare two documents as parsed JSON, so key order and whitespace never count."""
    if a is None or b is None:
        return a is b
    return canonical_json(a)[0] == canonical_json(b)[0]


async def _insert_reconcile_version(
    *,
    user_id: str,
    workspace_id: str,
    doc_key: str,
    base_version_no: int | None,
    content: dict[str, Any],
    content_sha256: str,
    reason: str,
    warnings: list[dict[str, Any]],
    schema_valid: bool,
    edited_path: str,
) -> dict[str, Any] | None:
    """Append a ``direct_edit`` version only if the doc is still at ``base_version_no``.

    The CAS runs under ``insert_version``'s per-(workspace, doc) advisory lock, so two
    end-of-turn passes on different workers cannot both record the same edit; the loser
    gets ``None``.
    """
    try:
        return await db_knowledge.insert_version(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key=doc_key,
            base_version_no=base_version_no,
            content=content,
            content_sha256=content_sha256,
            ops=None,
            reason=reason,
            evidence_refs=[],
            source="direct_edit",
            warnings=warnings,
            schema_valid=schema_valid,
            edited_path=edited_path,
            enforce_base=True,
        )
    except db_knowledge.VersionConflict as vc:
        logger.info(
            "[dalton.reconcile] skip %s in workspace %s: %s", doc_key, workspace_id, vc
        )
        return None


_ALERT_LINE_PREFIX = "**提示**："


def _block_shows_different_alerts(
    existing_agent_md: str | None, alerts: list[str]
) -> bool:
    """Whether the managed block's ``提示`` line differs from ``alerts`` as
    rendered today (codex r3 #5). Alerts are read back from the block itself —
    the one place that knows what the agent currently SEES — so a rewrite can
    both surface new alerts and CLEAR stale ones (e.g. a violation file the
    user deleted). No managed block or no change → False: a workspace with
    nothing new is never rewritten."""
    if not existing_agent_md:
        return False
    in_block = False
    current: str | None = None
    for line in existing_agent_md.splitlines():
        if line.startswith(AGENT_SUMMARY_START_PREFIX):
            in_block = True
            continue
        if AGENT_SUMMARY_END in line:
            break
        if in_block and line.startswith(_ALERT_LINE_PREFIX):
            current = line[len(_ALERT_LINE_PREFIX) :].strip()
            break
    return (current or "") != format_alert_lines(alerts)


def _subject_label(latest_valid_all: dict[str, Any], profile: str) -> str:
    """The rendering label for this workspace's docs. The company ticker when
    the workspace IS a company; the sector name only when the workspace's
    profile is sector — a residual sector row must never relabel a company
    workspace (codex r1 #3)."""
    company_doc = ((latest_valid_all.get("company") or {}).get("content")) or {}
    ticker = str(company_doc.get("ticker") or "").upper()
    if ticker or profile != "sector":
        return ticker
    sector_doc = ((latest_valid_all.get("sector") or {}).get("content")) or {}
    return str(sector_doc.get("name") or "").strip()


async def detect_direct_edits(
    workspace_id: str,
    user_id: str,
    *,
    storage_owner_id: str | None = None,
    skip_access_check: bool = False,
) -> dict[str, Any]:
    """Inspect the Postgres workspace_files mirror after a turn and record any direct edits (PK.5).

    Never blocks or reverts user/agent writes:
    - Direct edits to ``data/<doc>.json`` are saved as ``source='direct_edit'`` versions
      (with ``schema_valid=False`` if invalid).
    - Direct edits to rendered ``<doc>.md`` are recorded with ``rendered_md_edited`` warning so
      ``apply_pending`` backs up the edited markdown to ``history/`` on the next turn.
    - Tampering or deletion of ``published/**`` files is logged and surfaced as a warning.

    The scanned doc set follows the workspace's knowledge profile (P1): the
    historic eight for a company workspace, the sector set for a sector
    workspace. With the flag off the profile is always company — today's scan.
    In a sector workspace, a directly written ``data/company.json`` /
    ``data/management.json`` (docs the profile refuses) is recorded as an
    INVALID direct-edit version with a ``doc_not_allowed_for_profile`` warning,
    so it surfaces through the existing alert mechanism without ever becoming a
    valid version. If the coverage identity cannot be resolved, the six SHARED
    docs (identical in both profiles) are still scanned; only the three
    identity-bound docs (company/management/sector) are skipped for this pass
    (codex r2 #1) — never a company fallback.
    """
    eff_storage_owner = storage_owner_id or user_id
    if not skip_access_check and storage_owner_id is None:
        await verify_workspace_access(workspace_id, user_id)
    profile_known = True
    try:
        profile = await resolve_workspace_profile(workspace_id)
    except Exception as e:
        logger.warning(
            "[dalton.reconcile] coverage identity resolution failed for %s (%s); "
            "scanning shared docs only this pass (no company fallback)",
            workspace_id,
            e,
        )
        profile, profile_known = PROFILE_COMPANY, False
    meta = await get_file_metadata_for_sync(workspace_id)
    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    latest_valid_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )

    recorded_versions: list[dict[str, Any]] = []
    published_alerts: list[str] = []

    ticker = _subject_label(latest_valid_all, profile)
    scan_docs = doc_keys_for_profile(profile) if profile_known else SHARED_DOC_KEYS

    for doc_key in scan_docs:
        latest = latest_all.get(doc_key)
        latest_valid = latest_valid_all.get(doc_key)
        j_path = data_path(doc_key)
        j_meta = meta.get(j_path)

        # 行业层 P2（codex r3 #1 + r4 #1）：最新版本还没落盘
        # （files_written_at IS NULL，联动/总监 retry 以 backend=None 写入）
        # 时，文件必然落后于数据库——直接把哈希差当编辑会以 vN+1 为基线
        # 覆盖待落盘版本。但也不能整轮跳过：文件若另有真实修改，跳过会让
        # 下一回合 apply_pending 用 vN+1 覆盖掉修改（codex r4 #1）。因此只
        # 在"文件仍 == 最近已落盘基线"（same_content 规范化比较）时跳过，
        # 留给下一回合落盘；文件有真实修改、或没有已落盘基线时，走改动前
        # 的原检测逻辑——记出的 direct_edit 让 apply_pending 走"不覆盖
        # JSON"分支保住修改。渲染 md 检测本就兼容未落盘版本（下方
        # expected_rows 会并入最近已落盘基线），两种结局下都照常执行。
        # 开关关闭时本闸不存在，行为逐字不变。已知残余：待落盘内容与真实
        # 修改如何"合并"成一份，属于 backlog 里"工具写入覆盖未入账直接
        # 编辑"的通用问题，本闸只保证不新增丢失。
        skip_json_check = False
        if (
            is_coverage_units_enabled()
            and latest is not None
            and latest.get("files_written_at") is None
            and j_meta is not None
            and j_meta.get("kind") == "file"
        ):
            baseline_row = await db_knowledge.latest_written_version(
                workspace_id, doc_key
            )
            file_matches_baseline = False
            if baseline_row is not None:
                file_row = await db_get_file(workspace_id, j_path, include_content=True)
                raw_text = (
                    await resolve_file_text_or_none(
                        file_row,
                        user_id=eff_storage_owner,
                        context=f"reconcile:{j_path}",
                    )
                    if file_row
                    else None
                )
                if raw_text is not None:
                    try:
                        file_matches_baseline = same_content(
                            json.loads(raw_text), baseline_row.get("content")
                        )
                    except Exception:
                        file_matches_baseline = False
            if file_matches_baseline:
                skip_json_check = True
                logger.debug(
                    "[dalton.reconcile] %s in workspace %s has an unwritten latest "
                    "version (v%s) and the file still equals the last written "
                    "baseline (v%s); skipping direct-edit detection this pass",
                    doc_key,
                    workspace_id,
                    latest.get("version_no"),
                    baseline_row.get("version_no"),
                )

        # 1. Check data/<doc>.json
        if j_meta and j_meta.get("kind") == "file" and not skip_json_check:
            mirror_sha = j_meta.get("content_hash")
            if latest is None or mirror_sha != latest.get("content_sha256"):
                file_row = await db_get_file(workspace_id, j_path, include_content=True)
                raw_text = (
                    await resolve_file_text_or_none(
                        file_row,
                        user_id=eff_storage_owner,
                        context=f"reconcile:{j_path}",
                    )
                    if file_row
                    else None
                )
                if raw_text is not None:
                    raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
                    try:
                        parsed = json.loads(raw_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("top-level JSON value must be an object")
                        content_to_store = parsed
                        parse_error = None
                        # Canonical, so the hash names the content, not the file's bytes.
                        _, store_sha = canonical_json(parsed)
                        unchanged = latest is not None and same_content(
                            parsed, latest.get("content")
                        )
                    except Exception as parse_err:
                        content_to_store = {"_raw": raw_text[:65536]}
                        parse_error = parse_err
                        # ``_raw`` is truncated, so only the full-text hash can tell
                        # two invalid files apart.
                        store_sha = raw_sha
                        unchanged = (
                            latest is not None
                            and isinstance(latest.get("content"), dict)
                            and "_raw" in latest["content"]
                            and latest.get("content_sha256") == raw_sha
                        )

                    if unchanged:
                        # Only the bytes differ (key order, whitespace, or a hash taken
                        # from another serialization): not an edit.
                        content_to_store = None

                    if content_to_store is not None:
                        if parse_error is None:
                            old_valid_content = (
                                latest_valid["content"] if latest_valid else None
                            )
                            val = validate_update(
                                doc_key,
                                old_valid_content,
                                content_to_store,
                                source="direct_edit",
                                reason="direct_edit",
                            )
                            schema_valid = val.ok
                            warnings = [*val.errors, *val.warnings]
                        else:
                            schema_valid = False
                            warnings = [
                                {
                                    "code": "invalid_json",
                                    "path": "/",
                                    "message": str(parse_error),
                                }
                            ]

                        new_row = await _insert_reconcile_version(
                            user_id=user_id,
                            workspace_id=workspace_id,
                            doc_key=doc_key,
                            base_version_no=latest["version_no"] if latest else None,
                            content=content_to_store,
                            content_sha256=store_sha,
                            reason="检测到未经 knowledge_update 的直接修改（文件面板或命令行）",
                            warnings=warnings,
                            schema_valid=schema_valid,
                            edited_path=j_path,
                        )
                        if new_row is None:
                            # A concurrent pass (or a tool write) moved the doc on;
                            # whoever got there first owns this doc for this round.
                            continue
                        recorded_versions.append(new_row)
                        latest = new_row
                        if schema_valid:
                            latest_valid = new_row
                            # 行业层 P2（codex r1 #6）：直接编辑产生的有效
                            # thesis 版本同样触发跨单元联动——否则手改支柱/
                            # 反证只入账不传播，下一次工具更新已以它为旧
                            # 文档，事件永远补发不了。开关关闭零调用。
                            if doc_key == "thesis" and is_coverage_units_enabled():
                                try:
                                    from src.dalton.knowledge.cross_unit import (
                                        propagate_thesis_change,
                                    )

                                    linkage_effects = await propagate_thesis_change(
                                        user_id=user_id,
                                        workspace_id=workspace_id,
                                        old_doc=old_valid_content,
                                        new_doc=new_row.get("content"),
                                        new_version_no=int(new_row["version_no"]),
                                        profile=profile,
                                    )
                                    for effect in linkage_effects:
                                        if effect.get("status") == "failed":
                                            from src.dalton.knowledge.cross_unit import (
                                                persist_failed_linkage,
                                            )

                                            await persist_failed_linkage(user_id, effect)
                                except Exception as exc:
                                    logger.warning(
                                        "[dalton.reconcile] thesis linkage after direct"
                                        " edit failed for %s: %s",
                                        workspace_id,
                                        exc,
                                    )

        # 2. Check rendered <doc>.md
        md_rel = rendered_path(doc_key)
        if md_rel and latest_valid is not None:
            md_meta = meta.get(md_rel)
            actual_sha = (md_meta or {}).get("content_hash")
            if md_meta and md_meta.get("kind") == "file" and actual_sha:
                expected_rows = [latest_valid]
                if latest_valid.get("files_written_at") is None:
                    # Its re-render is still pending (apply_pending runs at the next
                    # turn start), so the file legitimately shows the last version
                    # whose files were written.
                    written = await db_knowledge.latest_written_valid_version(
                        workspace_id, doc_key
                    )
                    if written is not None:
                        expected_rows.append(written)
                expected_shas = {
                    hashlib.sha256(md.encode("utf-8")).hexdigest()
                    for md in (
                        render_document_md(
                            doc_key,
                            row["content"],
                            version_no=int(row["version_no"]),
                            at=row.get("created_at"),
                            ticker=ticker,
                        )
                        for row in expected_rows
                    )
                    if md
                }
                already_flagged = (
                    latest is not None
                    and latest.get("edited_path") == md_rel
                    and any(
                        isinstance(w, dict) and w.get("md_sha256") == actual_sha
                        for w in (latest.get("warnings") or [])
                    )
                )
                if expected_shas and actual_sha not in expected_shas and not already_flagged:
                    md_row = await _insert_reconcile_version(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        doc_key=doc_key,
                        base_version_no=latest["version_no"] if latest else None,
                        content=latest_valid["content"],
                        content_sha256=latest_valid["content_sha256"],
                        reason="渲染文件被直接修改；修改内容已存入 history/，请用 knowledge_update 合入",
                        warnings=[
                            {
                                "code": "rendered_md_edited",
                                "path": md_rel,
                                "md_sha256": actual_sha,
                                "message": f"{md_rel} was directly modified",
                            }
                        ],
                        schema_valid=True,
                        edited_path=md_rel,
                    )
                    if md_row is not None:
                        recorded_versions.append(md_row)

    # 2b. Sector profile: company-only docs are not in the scan set above, so a
    # directly written data/company.json / data/management.json would go
    # unnoticed. Record it as an INVALID direct-edit version carrying a
    # doc_not_allowed_for_profile warning — the existing alert mechanism then
    # surfaces it in agent.md, and it never becomes a valid version (codex r1 #2).
    if profile == PROFILE_SECTOR:
        for doc_key in ("company", "management"):
            j_path = data_path(doc_key)
            j_meta = meta.get(j_path)
            if not (j_meta and j_meta.get("kind") == "file"):
                continue
            latest = latest_all.get(doc_key)
            # codex r2 #3: dedup must consider the VIOLATION state, not just the
            # content. A same-content row that is already a violation record is
            # done; a same-content VALID row (flag-off era, or the r1 fallback
            # bug) still needs the violation recorded — otherwise the file
            # stays present with no alert at all.
            latest_is_violation = latest is not None and any(
                isinstance(w, dict) and w.get("code") == "doc_not_allowed_for_profile"
                for w in (latest.get("warnings") or [])
            )
            file_row = await db_get_file(workspace_id, j_path, include_content=True)
            raw_text = (
                await resolve_file_text_or_none(
                    file_row,
                    user_id=eff_storage_owner,
                    context=f"reconcile:{j_path}",
                )
                if file_row
                else None
            )
            if raw_text is None:
                continue
            raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            try:
                parsed = json.loads(raw_text)
                if not isinstance(parsed, dict):
                    raise ValueError("top-level JSON value must be an object")
                # Content comparison is canonical (not the file's bytes), so a
                # recorded row never re-fires per turn — but ONLY when that row
                # is itself the violation record.
                if latest_is_violation and same_content(parsed, latest.get("content")):
                    continue
                content_to_store = parsed
                _, store_sha = canonical_json(parsed)
            except Exception:
                content_to_store = {"_raw": raw_text[:65536]}
                store_sha = raw_sha
                if (
                    latest_is_violation
                    and latest is not None
                    and isinstance(latest.get("content"), dict)
                    and "_raw" in latest["content"]
                    and latest.get("content_sha256") == raw_sha
                ):
                    continue
            warning = {
                "code": "doc_not_allowed_for_profile",
                "path": j_path,
                "message": doc_not_allowed_message(doc_key, "sector"),
            }
            new_row = await _insert_reconcile_version(
                user_id=user_id,
                workspace_id=workspace_id,
                doc_key=doc_key,
                base_version_no=latest["version_no"] if latest else None,
                content=content_to_store,
                content_sha256=store_sha,
                reason="行业工作区出现不适用文档（公司专属），未按有效版本入库",
                warnings=[warning],
                schema_valid=False,
                edited_path=j_path,
            )
            if new_row is not None:
                recorded_versions.append(new_row)

    # 3. Check published/** files against dalton_publication_files
    pub_files = await db_publications.list_publication_files(workspace_id)
    for pf in pub_files:
        p_path = pf["path"]
        p_meta = meta.get(p_path)
        if not p_meta or p_meta.get("kind") != "file":
            msg = f"⚠ {p_path} 缺失（发布物按约定不改写）"
            logger.warning("[dalton.reconcile] %s in workspace %s", msg, workspace_id)
            published_alerts.append(msg)
        elif p_meta.get("content_hash") != pf["sha256"]:
            msg = f"⚠ {p_path} 与发布记录的哈希不一致（发布物按约定不改写）"
            logger.warning("[dalton.reconcile] %s in workspace %s", msg, workspace_id)
            published_alerts.append(msg)

    result: dict[str, Any] = {
        "recorded_versions": recorded_versions,
        "published_alerts": published_alerts,
    }
    if not profile_known:
        result["skipped"] = "coverage-identity-unavailable"
        result["skipped_docs"] = list(PROFILE_SPECIFIC_DOCS)
    return result


async def collect_workspace_alerts(
    workspace_id: str,
    *,
    profile: str | None = None,
) -> list[str]:
    """Derive active direct-edit and publication tampering alerts from Postgres.

    A ``doc_not_allowed_for_profile`` violation row stays in history forever,
    but its ALERT is active only while the offending file still exists in the
    mirror (codex r2 #4) — deleting the file IS the fix. And the fix hint for a
    sector workspace says so: ``knowledge_update`` on a company-only doc is
    always refused there, so pointing at it would send the agent into a wall.
    """
    alerts: list[str] = []
    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    if profile is None:
        try:
            profile = await resolve_workspace_profile(workspace_id)
        except Exception:
            # Display-layer fallback only: with the identity unknown, show the
            # historic company wording rather than guessing sector semantics.
            profile = PROFILE_COMPANY
    # Only a sector workspace has profile violations whose alert depends on
    # the file still existing; a company workspace makes no extra lookup.
    meta = (
        await get_file_metadata_for_sync(workspace_id)
        if profile == PROFILE_SECTOR
        else {}
    )
    for doc_key, row in sorted(latest_all.items()):
        if row.get("source") != "direct_edit":
            continue
        v_no = row["version_no"]
        edited_path = row.get("edited_path") or data_path(doc_key)
        is_profile_violation = any(
            isinstance(w, dict) and w.get("code") == "doc_not_allowed_for_profile"
            for w in (row.get("warnings") or [])
        )
        if is_profile_violation:
            if profile == PROFILE_SECTOR and (meta.get(edited_path) or {}).get(
                "kind"
            ) == "file":
                alerts.append(
                    f"⚠ {edited_path} 是行业工作区不使用的公司专属文档（违规已记录 v{v_no}）；"
                    f"修复：删除 {edited_path}（行业工作区不使用公司专属文档），"
                    "需要记录的内容放进 sector / thesis——不要用 knowledge_update 写它（会被拒绝）"
                )
            continue
        if not row.get("schema_valid", True):
            first_w = (row.get("warnings") or [{}])[0]
            detail = f"{first_w.get('path', '/')}: {first_w.get('message', 'invalid')}"
            alerts.append(
                f"⚠ {edited_path} 被直接修改（v{v_no}，不合 schema：{detail}），请用 knowledge_update 修正"
            )
        elif edited_path in RENDERED.values():
            created_at = row.get("created_at") or datetime.now(timezone.utc)
            if isinstance(created_at, str):
                try:
                    created_dt = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    created_dt = datetime.now(timezone.utc)
            else:
                created_dt = created_at
            backup_md = history_edited_md_path(doc_key, int(v_no), created_dt)
            alerts.append(
                f"ℹ {edited_path} 被直接修改，原文在 {backup_md}，请把要保留的内容用 knowledge_update 合入"
            )
    return alerts


async def apply_pending(
    workspace_id: str,
    backend: Any,
    *,
    user_id: str | None = None,
    storage_owner_id: str | None = None,
    skip_access_check: bool = False,
) -> dict[str, Any]:
    """Reconcile any unwritten versions and refresh ``agent.md`` at turn start (PK.5)."""
    if user_id is not None and not skip_access_check and storage_owner_id is None:
        await verify_workspace_access(workspace_id, user_id)
    if backend is None:
        return {"written": 0, "alerts": []}

    # Codex r1 #2/#3 + r2 #1: the profile decides rendering and gates the
    # profile-specific docs. With the identity unknown, SHARED docs (identical
    # in both profiles) still write — under the company label, a display-only
    # transient — while company/management/sector pending rows wait for the
    # registry to answer; never a company fallback for them.
    profile_known = True
    try:
        profile = await resolve_workspace_profile(workspace_id)
    except Exception as e:
        logger.warning(
            "[dalton.reconcile] coverage identity resolution failed for %s (%s); "
            "writing shared-doc pending versions only (no company fallback for "
            "profile-specific docs)",
            workspace_id,
            e,
        )
        profile, profile_known = PROFILE_COMPANY, False
    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    latest_valid_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )
    company_doc = ((latest_valid_all.get("company") or {}).get("content")) or {}
    # A residual sector row must not relabel a company workspace's rendering —
    # the sector content is only used when the workspace IS a sector (codex r1 #3).
    sector_doc = (
        ((latest_valid_all.get("sector") or {}).get("content") or {})
        if profile == PROFILE_SECTOR
        else {}
    )
    ticker = _subject_label(latest_valid_all, profile)

    written_count = 0
    for doc_key, row in latest_all.items():
        if row.get("files_written_at") is not None:
            continue
        if not profile_known and doc_key in PROFILE_SPECIFIC_DOCS:
            # Identity-bound doc with an unknown identity: leave pending, retry
            # next pass. Writing it could render or admit it under the wrong
            # profile; skipping it loses nothing (the version row exists).
            continue

        v_no = int(row["version_no"])
        created_at = row.get("created_at") or datetime.now(timezone.utc)
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                created_dt = datetime.now(timezone.utc)
        else:
            created_dt = created_at

        source = row.get("source")
        edited_path = row.get("edited_path")

        if source == "direct_edit" and edited_path == RENDERED.get(doc_key):
            # Back up the user's edited .md to history/<doc>/<UTC>-v<NNNN>-edited.md,
            # then re-render <doc>.md from latest_valid_version
            current_md = await backend.aread_text(edited_path)
            if current_md is not None:
                backup_path = history_edited_md_path(doc_key, v_no, created_dt)
                await backend.awrite_text(backup_path, current_md)
            valid_row = latest_valid_all.get(doc_key) or row
            md_text = render_document_md(
                doc_key,
                valid_row["content"],
                version_no=int(valid_row["version_no"]),
                at=valid_row.get("created_at"),
                ticker=ticker,
            )
            if md_text:
                await backend.awrite_text(edited_path, md_text)
            await db_knowledge.mark_files_written(str(row["knowledge_version_id"]))
            written_count += 1

        elif source == "direct_edit" and edited_path == data_path(doc_key):
            # Do NOT overwrite data/<doc>.json; write history snapshot; re-render md only if valid
            json_text, _ = canonical_json(row["content"])
            h_path = history_path(doc_key, v_no, created_dt)
            await backend.awrite_text(h_path, json_text)
            md_rel = rendered_path(doc_key)
            if md_rel and row.get("schema_valid", True):
                md_text = render_document_md(
                    doc_key,
                    row["content"],
                    version_no=v_no,
                    at=created_dt,
                    ticker=ticker,
                )
                if md_text:
                    await backend.awrite_text(md_rel, md_text)
            await db_knowledge.mark_files_written(str(row["knowledge_version_id"]))
            written_count += 1

        else:
            # Tool / import / publish version that had files_written_at == NULL
            await write_version_files_to_backend(
                workspace_id, row, backend, profile=profile
            )
            written_count += 1

    alerts = await collect_workspace_alerts(workspace_id, profile=profile)
    # A sector workspace's violation alert disappears from the ledger once its
    # file is deleted, yet the managed block still shows it — so with nothing
    # else to write, a sector workspace reads the block to see whether it must
    # clear a stale alert (codex r3 #5). A company workspace keeps the old
    # path exactly: no extra read when there is nothing to write.
    existing_agent_md: str | None = None
    stale_alert = False
    if written_count == 0 and not alerts and profile == PROFILE_SECTOR:
        existing_agent_md = await backend.aread_text(AGENT_MD)
        stale_alert = _block_shows_different_alerts(existing_agent_md, alerts)
    if written_count > 0 or alerts or stale_alert:
        if existing_agent_md is None:
            existing_agent_md = await backend.aread_text(AGENT_MD)
        thesis_row = latest_valid_all.get("thesis")
        debate_row = latest_valid_all.get("debate")
        agenda_row = latest_valid_all.get("agenda")
        models_row = latest_valid_all.get("models")
        recent_verdicts = await db_verdicts.list_verdicts(workspace_id, limit=1)
        last_verdict = recent_verdicts[0] if recent_verdicts else None
        block = render_agent_block(
            ticker=ticker or "COMPANY",
            company_doc=company_doc,
            sector_doc=sector_doc or None,
            thesis_doc=(thesis_row or {}).get("content"),
            thesis_version_no=(thesis_row or {}).get("version_no"),
            debate_doc=(debate_row or {}).get("content"),
            agenda_doc=(agenda_row or {}).get("content"),
            models_doc=(models_row or {}).get("content"),
            last_verdict=last_verdict,
            alerts=alerts,
        )
        new_agent_md = update_agent_md_block(existing_agent_md, block)
        await backend.awrite_text(AGENT_MD, new_agent_md)

    result: dict[str, Any] = {"written": written_count, "alerts": alerts}
    if not profile_known:
        result["skipped"] = "coverage-identity-unavailable"
        result["skipped_docs"] = list(PROFILE_SPECIFIC_DOCS)
    return result


from langchain.agents.middleware import AgentMiddleware  # noqa: E402


class DaltonReconcileMiddleware(AgentMiddleware):
    """Turn-start middleware that runs ``apply_pending`` before the agent runs (PK.5)."""

    def __init__(
        self,
        *,
        workspace_id: str | None,
        backend: Any,
        user_id: str | None = None,
        storage_owner_id: str | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._backend = backend
        self._user_id = user_id
        self._storage_owner_id = storage_owner_id

    async def abefore_agent(
        self, state: Any, runtime: Any = None
    ) -> dict[str, Any] | None:
        if not self._workspace_id or self._backend is None:
            return None
        try:
            import asyncio

            from src.dalton.settings import get_dalton_settings

            if not get_dalton_settings().knowledge.enabled:
                return None
            await asyncio.wait_for(
                apply_pending(
                    self._workspace_id,
                    self._backend,
                    user_id=self._user_id,
                    storage_owner_id=self._storage_owner_id,
                    skip_access_check=True,
                ),
                timeout=10.0,
            )
        except Exception as e:
            logger.debug("[dalton.reconcile] abefore_agent skipped: %s", e)
        return None


