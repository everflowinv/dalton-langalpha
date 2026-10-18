"""Post-turn direct-edit detection and next-turn file reconciliation (PK.5)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import publications as db_publications
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge.paths import (
    AGENT_MD,
    DOC_KEYS,
    RENDERED,
    data_path,
    history_edited_md_path,
    history_path,
    rendered_path,
)
from src.dalton.knowledge.render import (
    render_agent_block,
    render_document_md,
    update_agent_md_block,
)
from src.dalton.knowledge.service import canonical_json, write_version_files_to_backend
from src.dalton.knowledge.validate import validate_update
from src.server.database.workspace_file import (
    get_file as db_get_file,
    get_file_metadata_for_sync,
)
from src.server.services.persistence.resolve import resolve_file_text_or_none

logger = logging.getLogger(__name__)


async def detect_direct_edits(
    workspace_id: str, user_id: str
) -> dict[str, Any]:
    """Inspect the Postgres workspace_files mirror after a turn and record any direct edits (PK.5).

    Never blocks or reverts user/agent writes:
    - Direct edits to ``data/<doc>.json`` are saved as ``source='direct_edit'`` versions
      (with ``schema_valid=False`` if invalid).
    - Direct edits to rendered ``<doc>.md`` are recorded with ``rendered_md_edited`` warning so
      ``apply_pending`` backs up the edited markdown to ``history/`` on the next turn.
    - Tampering or deletion of ``published/**`` files is logged and surfaced as a warning.
    """
    meta = await get_file_metadata_for_sync(workspace_id)
    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    latest_valid_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )

    recorded_versions: list[dict[str, Any]] = []
    published_alerts: list[str] = []

    company_doc = ((latest_valid_all.get("company") or {}).get("content")) or {}
    ticker = str(company_doc.get("ticker") or "").upper()

    for doc_key in DOC_KEYS:
        latest = latest_all.get(doc_key)
        latest_valid = latest_valid_all.get(doc_key)
        j_path = data_path(doc_key)
        j_meta = meta.get(j_path)

        # 1. Check data/<doc>.json
        if j_meta and j_meta.get("kind") == "file":
            mirror_sha = j_meta.get("content_hash")
            if latest is None or mirror_sha != latest.get("content_sha256"):
                file_row = await db_get_file(workspace_id, j_path, include_content=True)
                raw_text = (
                    await resolve_file_text_or_none(
                        file_row, user_id=user_id, context=f"reconcile:{j_path}"
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
                        old_valid_content = (
                            latest_valid["content"] if latest_valid else None
                        )
                        val = validate_update(
                            doc_key,
                            old_valid_content,
                            parsed,
                            source="direct_edit",
                            reason="direct_edit",
                        )
                        schema_valid = val.ok
                        warnings = [*val.errors, *val.warnings]
                        content_to_store = parsed
                    except Exception as parse_err:
                        schema_valid = False
                        warnings = [
                            {
                                "code": "invalid_json",
                                "path": "/",
                                "message": str(parse_err),
                            }
                        ]
                        content_to_store = {"_raw": raw_text[:65536]}

                    new_row = await db_knowledge.insert_version(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        doc_key=doc_key,
                        base_version_no=latest["version_no"] if latest else None,
                        content=content_to_store,
                        content_sha256=mirror_sha or raw_sha,
                        ops=None,
                        reason="检测到未经 knowledge_update 的直接修改（文件面板或命令行）",
                        evidence_refs=[],
                        source="direct_edit",
                        warnings=warnings,
                        schema_valid=schema_valid,
                        edited_path=j_path,
                    )
                    recorded_versions.append(new_row)
                    latest = new_row
                    if schema_valid:
                        latest_valid = new_row

        # 2. Check rendered <doc>.md
        md_rel = rendered_path(doc_key)
        if md_rel and latest_valid is not None:
            md_meta = meta.get(md_rel)
            if md_meta and md_meta.get("kind") == "file":
                expected_md = render_document_md(
                    doc_key,
                    latest_valid["content"],
                    version_no=int(latest_valid["version_no"]),
                    at=latest_valid.get("created_at"),
                    ticker=ticker,
                )
                if expected_md:
                    expected_sha = hashlib.sha256(
                        expected_md.encode("utf-8")
                    ).hexdigest()
                    actual_sha = md_meta.get("content_hash")
                    already_flagged = (
                        latest is not None
                        and latest.get("edited_path") == md_rel
                        and any(
                            isinstance(w, dict)
                            and w.get("md_sha256") == actual_sha
                            for w in (latest.get("warnings") or [])
                        )
                    )
                    if actual_sha and actual_sha != expected_sha and not already_flagged:
                        md_row = await db_knowledge.insert_version(
                            user_id=user_id,
                            workspace_id=workspace_id,
                            doc_key=doc_key,
                            base_version_no=latest["version_no"] if latest else None,
                            content=latest_valid["content"],
                            content_sha256=latest_valid["content_sha256"],
                            ops=None,
                            reason="渲染文件被直接修改；修改内容已存入 history/，请用 knowledge_update 合入",
                            evidence_refs=[],
                            source="direct_edit",
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
                        recorded_versions.append(md_row)

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

    return {
        "recorded_versions": recorded_versions,
        "published_alerts": published_alerts,
    }


async def collect_workspace_alerts(workspace_id: str) -> list[str]:
    """Derive active direct-edit and publication tampering alerts from Postgres."""
    alerts: list[str] = []
    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    for doc_key, row in sorted(latest_all.items()):
        if row.get("source") != "direct_edit":
            continue
        v_no = row["version_no"]
        edited_path = row.get("edited_path") or data_path(doc_key)
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
) -> dict[str, Any]:
    """Reconcile any unwritten versions and refresh ``agent.md`` at turn start (PK.5)."""
    if backend is None:
        return {"written": 0, "alerts": []}

    latest_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=False
    )
    latest_valid_all = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )
    company_doc = ((latest_valid_all.get("company") or {}).get("content")) or {}
    ticker = str(company_doc.get("ticker") or "").upper()

    written_count = 0
    for doc_key, row in latest_all.items():
        if row.get("files_written_at") is not None:
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
            await write_version_files_to_backend(workspace_id, row, backend)
            written_count += 1

    alerts = await collect_workspace_alerts(workspace_id)
    if written_count > 0 or alerts:
        thesis_row = latest_valid_all.get("thesis")
        debate_row = latest_valid_all.get("debate")
        agenda_row = latest_valid_all.get("agenda")
        models_row = latest_valid_all.get("models")
        recent_verdicts = await db_verdicts.list_verdicts(workspace_id, limit=1)
        last_verdict = recent_verdicts[0] if recent_verdicts else None
        block = render_agent_block(
            ticker=ticker or "COMPANY",
            company_doc=company_doc,
            thesis_doc=(thesis_row or {}).get("content"),
            thesis_version_no=(thesis_row or {}).get("version_no"),
            debate_doc=(debate_row or {}).get("content"),
            agenda_doc=(agenda_row or {}).get("content"),
            models_doc=(models_row or {}).get("content"),
            last_verdict=last_verdict,
            alerts=alerts,
        )
        existing_agent_md = await backend.aread_text(AGENT_MD)
        new_agent_md = update_agent_md_block(existing_agent_md, block)
        await backend.awrite_text(AGENT_MD, new_agent_md)

    return {"written": written_count, "alerts": alerts}


from langchain.agents.middleware import AgentMiddleware  # noqa: E402


class DaltonReconcileMiddleware(AgentMiddleware):
    """Turn-start middleware that runs ``apply_pending`` before the agent runs (PK.5)."""

    def __init__(
        self,
        *,
        workspace_id: str | None,
        backend: Any,
        user_id: str | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._backend = backend
        self._user_id = user_id

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
                ),
                timeout=10.0,
            )
        except Exception as e:
            logger.debug("[dalton.reconcile] abefore_agent skipped: %s", e)
        return None


