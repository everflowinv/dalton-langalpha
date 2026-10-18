"""Company knowledge layer service: apply updates, append verdicts, render files, and import (PK.4, PK.6, PK.7)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.database.knowledge import VersionConflict
from src.dalton.knowledge.ops import OpError, apply_ops, diff_documents
from src.dalton.knowledge.paths import (
    AGENT_MD,
    DOC_KEYS,
    VERDICTS_MD,
    data_path,
    history_path,
    rendered_path,
)
from src.dalton.knowledge.render import (
    VERDICT_ZH,
    render_agent_block,
    render_document_md,
    render_verdicts_md,
    update_agent_md_block,
)
from src.dalton.knowledge.validate import validate_update, validate_verdict

logger = logging.getLogger(__name__)

MAX_AGENT_MD_WARN_CHARS = int(32768 * 0.85)


def canonical_json(doc: dict[str, Any]) -> tuple[str, str]:
    """Serialize ``doc`` canonically (indent=2, UTF-8, trailing newline) and return ``(text, sha256)``."""
    text = json.dumps(doc, sort_keys=False, ensure_ascii=False, indent=2) + "\n"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


def _normalize_legacy_aliases(doc_key: str, doc: dict[str, Any]) -> None:
    """Normalize common field aliases (e.g. model_ref{file,sheet,cell}) in-place before schema validation."""
    if doc_key == "thesis":
        if "evidence_log" in doc and "evidence" not in doc:
            doc["evidence"] = doc.pop("evidence_log")
        hv = doc.get("house_view")
        if isinstance(hv, dict) and isinstance(hv.get("target"), dict):
            mref = hv["target"].get("model_ref")
            if isinstance(mref, dict):
                if "file" in mref and "path" not in mref:
                    mref["path"] = mref.pop("file")
                if "sheet" in mref:
                    sheet = mref.pop("sheet")
                    cell = mref.get("cell") or ""
                    if sheet and cell and "!" not in str(cell):
                        mref["cell"] = f"{sheet}!{cell}"
                mref.setdefault("kind", "model_cell")


async def _populate_model_sha256(doc: dict[str, Any], backend: Any) -> None:
    """Compute sha256 for any model version in ``models.json`` when ``backend`` is available."""
    if backend is None:
        return
    for v in doc.get("versions") or []:
        if not isinstance(v, dict) or v.get("sha256"):
            continue
        mpath = v.get("path")
        if not mpath:
            continue
        try:
            if hasattr(backend, "adownload_file_bytes"):
                raw = await backend.adownload_file_bytes(mpath)
                if raw is not None:
                    v["sha256"] = hashlib.sha256(raw).hexdigest()
        except Exception as e:
            logger.debug("Could not compute model sha256 for %s: %s", mpath, e)


async def _sync_workspace_description(
    workspace_id: str, company_doc: dict[str, Any]
) -> None:
    """Sync workspace description when ``company.json`` is updated (PK.1 / PK.4)."""
    try:
        from src.server.database.workspace import update_workspace

        name = company_doc.get("name") or company_doc.get("ticker") or ""
        tier = str(company_doc.get("coverage_tier") or "core").capitalize()
        one_liner = company_doc.get("one_liner") or ""
        desc = " · ".join(part for part in (name, tier, one_liner) if part)
        if desc:
            await update_workspace(workspace_id, description=desc[:500])
    except Exception as e:
        logger.warning("Failed to sync workspace description for %s: %s", workspace_id, e)


async def write_version_files_to_backend(
    workspace_id: str,
    version_row: dict[str, Any],
    backend: Any,
    *,
    alerts: list[str] | None = None,
) -> list[str]:
    """Write ``data/<doc>.json``, ``history/<doc>/...``, rendered ``<doc>.md``, and ``agent.md`` block."""
    notes: list[str] = []
    if backend is None:
        return notes

    doc_key = version_row["doc_key"]
    version_no = int(version_row["version_no"])
    content = version_row["content"]
    created_at = version_row.get("created_at") or datetime.now(timezone.utc)
    if isinstance(created_at, str):
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_dt = datetime.now(timezone.utc)
    else:
        created_dt = created_at

    json_text, _ = canonical_json(content)

    # 1. Write data/<doc>.json unless this is a direct_edit of that file
    if not (
        version_row.get("source") == "direct_edit"
        and version_row.get("edited_path") == data_path(doc_key)
    ):
        await backend.awrite_text(data_path(doc_key), json_text)

    # 2. Write history/<doc>/<UTC>-v<NNNN>.json
    h_path = history_path(doc_key, version_no, created_dt)
    await backend.awrite_text(h_path, json_text)

    # 3. Fetch all latest valid docs to determine ticker and render <doc>.md + agent.md
    latest_map = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )
    if version_row.get("schema_valid", True):
        latest_map[doc_key] = version_row

    company_doc = (latest_map.get("company") or {}).get("content") or {}
    ticker = str(company_doc.get("ticker") or content.get("ticker") or "").upper()

    md_rel = rendered_path(doc_key)
    if md_rel and version_row.get("schema_valid", True):
        md_text = render_document_md(
            doc_key,
            content,
            version_no=version_no,
            at=created_dt,
            ticker=ticker,
        )
        if md_text:
            await backend.awrite_text(md_rel, md_text)

    # 4. Update agent.md managed block
    thesis_row = latest_map.get("thesis")
    debate_row = latest_map.get("debate")
    agenda_row = latest_map.get("agenda")
    models_row = latest_map.get("models")
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
    if len(new_agent_md) > MAX_AGENT_MD_WARN_CHARS:
        notes.append(
            f"agent.md length ({len(new_agent_md)} chars) exceeds 85% of MAX_AGENT_MD_SIZE (32768); "
            "please compress free-form notes outside the managed block."
        )

    await db_knowledge.mark_files_written(str(version_row["knowledge_version_id"]))
    return notes


async def apply_update(
    *,
    user_id: str,
    workspace_id: str,
    doc_key: str,
    ops: list[dict[str, Any]],
    reason: str,
    base_version: int | None,
    evidence_refs: list[dict[str, Any]] | None = None,
    source: str = "tool",
    backend: Any = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute the full PK.4 knowledge update pipeline: validate -> DB version -> write files."""
    if doc_key not in DOC_KEYS:
        return {
            "ok": False,
            "error": "invalid_doc_key",
            "message": f"Unknown doc {doc_key!r}; expected one of {DOC_KEYS}",
        }

    # Step 1: Fetch latest version from DB. If none exists and data/<doc>.json is already in sandbox,
    # record it as a direct_edit v1 first (PK.4 step 1).
    latest = await db_knowledge.latest_version(workspace_id, doc_key)
    if latest is None and backend is not None and source == "tool":
        existing_raw = await backend.aread_text(data_path(doc_key))
        if existing_raw:
            try:
                parsed = json.loads(existing_raw)
                val_existing = validate_update(
                    doc_key, None, parsed, source="direct_edit", reason="initial file"
                )
                _, sha = canonical_json(parsed)
                latest = await db_knowledge.insert_version(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    doc_key=doc_key,
                    base_version_no=None,
                    content=parsed,
                    content_sha256=sha,
                    ops=None,
                    reason="检测到工作区已有的初始文件（direct_edit）",
                    evidence_refs=[],
                    source="direct_edit",
                    warnings=[*val_existing.errors, *val_existing.warnings],
                    schema_valid=val_existing.ok,
                    edited_path=data_path(doc_key),
                )
            except Exception as e:
                logger.warning("Could not import existing %s as direct_edit: %s", doc_key, e)

    # Step 2: Check base_version for tool / import paths
    latest_no = latest["version_no"] if latest else None
    if source == "tool" and base_version != latest_no:
        return {
            "ok": False,
            "error": "version_conflict",
            "latest_version": latest_no,
            "latest_reason": latest.get("reason") if latest else None,
            "message": (
                f"version_conflict on {doc_key}: passed base_version={base_version}, "
                f"but latest version in DB is v{latest_no}. Re-read data/{doc_key}.json and retry."
            ),
        }
    if source == "import" and latest is not None:
        return {
            "ok": False,
            "error": "already_exists",
            "latest_version": latest_no,
            "message": f"{doc_key} already exists at v{latest_no}, skipping import.",
        }

    base_doc = (
        copy.deepcopy(latest["content"])
        if (latest and isinstance(latest.get("content"), dict) and "_raw" not in latest["content"])
        else None
    )

    # Step 3: Apply ops -> server fields -> validate schema & 3 hard invariants
    try:
        new_doc, op_summaries = apply_ops(
            doc_key,
            base_doc,
            ops,
            reason=reason,
            now=now,
        )
    except OpError as e:
        return {
            "ok": False,
            "error": e.code,
            "problems": [{"code": e.code, "path": e.path, "message": e.message}],
            "message": f"- {e.path}: {e.code} ({e.message})",
        }

    _normalize_legacy_aliases(doc_key, new_doc)
    if doc_key == "models":
        await _populate_model_sha256(new_doc, backend)

    models_doc: dict[str, Any] | None = None
    if doc_key == "thesis":
        models_row = await db_knowledge.latest_valid_version(workspace_id, "models")
        if models_row and isinstance(models_row.get("content"), dict):
            models_doc = models_row["content"]

    # Compare against latest valid version for hard invariants (I1/I2)
    valid_base_doc = base_doc
    if latest and not latest.get("schema_valid", True):
        valid_row = await db_knowledge.latest_valid_version(workspace_id, doc_key)
        valid_base_doc = valid_row["content"] if valid_row else None

    val = validate_update(
        doc_key,
        valid_base_doc,
        new_doc,
        source=source,
        models_doc=models_doc,
        reason=reason,
    )
    if not val.ok:
        return {
            "ok": False,
            "error": val.errors[0]["code"],
            "problems": val.errors,
            "warnings": val.warnings,
            "message": f"Validation failed for {doc_key} (NOT SAVED):\n{val.format_problems()}",
        }

    # Step 4: Insert version into DB
    _, content_sha = canonical_json(new_doc)
    try:
        version_row = await db_knowledge.insert_version(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key=doc_key,
            base_version_no=base_version,
            content=new_doc,
            content_sha256=content_sha,
            ops=ops,
            reason=reason,
            evidence_refs=evidence_refs or [],
            source=source,
            warnings=val.warnings,
            schema_valid=True,
            edited_path=None,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
    except VersionConflict as vc:
        curr_no = vc.latest["version_no"] if vc.latest else None
        return {
            "ok": False,
            "error": "version_conflict",
            "latest_version": curr_no,
            "message": (
                f"version_conflict on {doc_key}: passed base_version={base_version}, "
                f"but latest version in DB is v{curr_no}. Re-read data/{doc_key}.json and retry."
            ),
        }

    # Step 5 & 6: Write files to sandbox (failures do not roll back DB; files_written_at stays NULL)
    extra_notes: list[str] = []
    if doc_key == "company":
        await _sync_workspace_description(workspace_id, new_doc)

    if backend is not None:
        try:
            extra_notes = await write_version_files_to_backend(
                workspace_id, version_row, backend
            )
        except Exception as e:
            logger.warning(
                "Sandbox file write deferred for %s v%s: %s",
                doc_key,
                version_row["version_no"],
                e,
            )

    new_no = version_row["version_no"]
    was_str = f" (was v{latest_no})" if latest_no is not None else " (initial)"
    ops_str = "; ".join(op_summaries[:5])
    md_file = rendered_path(doc_key)
    render_str = f" Rendered {md_file}." if md_file else ""
    warn_str = ""
    if val.warnings or extra_notes:
        all_w = [
            f"{w['code']}: {w['message']}" for w in val.warnings[:5]
        ] + extra_notes
        warn_str = f" {len(all_w)} warning(s): " + "; ".join(all_w)

    msg = f"Saved {doc_key} v{new_no}{was_str}: {ops_str}.{render_str}{warn_str}"
    return {
        "ok": True,
        "doc": doc_key,
        "version_no": new_no,
        "previous_version_no": latest_no,
        "knowledge_version_id": version_row["knowledge_version_id"],
        "content_sha256": content_sha,
        "rendered_file": md_file,
        "warnings": val.warnings,
        "message": msg,
    }


async def append_verdict(
    *,
    user_id: str,
    workspace_id: str,
    verdict: dict[str, Any],
    agenda_ops: list[dict[str, Any]] | None = None,
    backend: Any = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    origin_type: str | None = None,
    automation_execution_id: str | None = None,
) -> dict[str, Any]:
    """Validate and record an event verdict, update agenda, and render ``verdicts.md`` (PK.4)."""
    v_copy = copy.deepcopy(verdict)
    now_iso = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    v_copy.setdefault("at", now_iso)
    had_id = bool(v_copy.get("id"))
    if not had_id:
        # Temporary placeholder for schema check; real ID assigned in DB under advisory lock
        v_copy["id"] = f"VD-{now_iso[:10].replace('-', '')}-01"

    thesis_row = await db_knowledge.latest_valid_version(workspace_id, "thesis")
    thesis_doc = thesis_row["content"] if thesis_row else None
    val = validate_verdict(v_copy, thesis_doc=thesis_doc)
    if not val.ok:
        return {
            "ok": False,
            "error": "schema_error",
            "problems": val.errors,
            "message": f"Invalid verdict payload (NOT SAVED):\n{val.format_problems()}",
        }

    if not had_id:
        v_copy.pop("id", None)

    verdict_row = await db_verdicts.insert_verdict(
        user_id=user_id,
        workspace_id=workspace_id,
        verdict=v_copy,
        thread_id=thread_id,
        run_id=run_id,
        automation_execution_id=automation_execution_id,
        origin_type=origin_type,
    )
    verdict_id = verdict_row["verdict_id"]
    word = verdict_row["word"]

    # Automatically update agenda if an agenda document exists or agenda_ops were passed
    agenda_row = await db_knowledge.latest_version(workspace_id, "agenda")
    agenda_version_no: int | None = None
    if agenda_row is not None:
        combined_ops: list[dict[str, Any]] = [
            {
                "op": "set",
                "path": "/last_verdict",
                "value": {
                    "verdict_id": verdict_id,
                    "word": word,
                    "at": v_copy["at"],
                },
            }
        ]
        agenda_content = agenda_row.get("content") or {}
        # Mark matching validation as done if referenced
        val_id = (v_copy.get("next_validation") or {}).get("validation_id") or (
            v_copy.get("trigger") or {}
        ).get("ref")
        if val_id and any(
            isinstance(x, dict) and x.get("id") == val_id
            for x in (agenda_content.get("next_validations") or [])
        ):
            combined_ops.append(
                {"op": "set", "path": f"/next_validations/{val_id}/status", "value": "done"}
            )
            combined_ops.append(
                {
                    "op": "set",
                    "path": f"/next_validations/{val_id}/result_verdict_id",
                    "value": verdict_id,
                }
            )
        # Append attempt to referenced question_ids
        for qid in v_copy.get("question_ids") or []:
            if any(
                isinstance(q, dict) and q.get("id") == qid
                for q in (agenda_content.get("open_questions") or [])
            ):
                att: dict[str, Any] = {
                    "at": v_copy["at"],
                    "method": f"verdict {verdict_id}",
                    "progress": v_copy["progress"],
                    "note": v_copy["event"],
                }
                if thread_id:
                    att["thread_id"] = str(thread_id)
                combined_ops.append(
                    {
                        "op": "append",
                        "path": f"/open_questions/{qid}/attempts",
                        "value": att,
                    }
                )
        if agenda_ops:
            combined_ops.extend(agenda_ops)

        ag_res = await apply_update(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key="agenda",
            ops=combined_ops,
            reason=f"verdict {verdict_id}: {v_copy['event'][:80]}",
            base_version=agenda_row["version_no"],
            source="tool",
            backend=backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        if ag_res.get("ok"):
            agenda_version_no = ag_res.get("version_no")

    # Render verdicts.md in sandbox if backend is present
    if backend is not None:
        try:
            recent = await db_verdicts.list_verdicts(workspace_id, limit=60)
            company_row = await db_knowledge.latest_valid_version(
                workspace_id, "company"
            )
            ticker = (
                ((company_row or {}).get("content") or {}).get("ticker") or ""
            ).upper()
            v_md = render_verdicts_md(recent, ticker=ticker)
            await backend.awrite_text(VERDICTS_MD, v_md)
        except Exception as e:
            logger.warning("Failed to render verdicts.md for %s: %s", workspace_id, e)

    zh = VERDICT_ZH.get(word, word)
    warn_note = f" Warnings: {val.format_warnings()}" if val.warnings else ""
    ag_note = f"; updated agenda v{agenda_version_no}" if agenda_version_no else ""
    msg = f"Recorded verdict {verdict_id} ({word} / {zh}){ag_note}. Rendered {VERDICTS_MD}.{warn_note}"
    return {
        "ok": True,
        "verdict_id": verdict_id,
        "word": word,
        "agenda_version_no": agenda_version_no,
        "warnings": val.warnings,
        "message": msg,
    }


async def import_knowledge_doc(
    *,
    user_id: str,
    workspace_id: str,
    doc_key: str,
    content: dict[str, Any],
    reason: str,
    provenance: list[dict[str, Any]] | None = None,
    backend: Any = None,
) -> dict[str, Any]:
    """Import an initial company knowledge document with ``source='import'`` (PK.7)."""
    doc_copy = copy.deepcopy(content)
    if doc_key == "thesis":
        doc_copy["official"] = None
        for p in doc_copy.get("pillars") or []:
            if isinstance(p, dict):
                p["provenance"] = "inherited"
    return await apply_update(
        user_id=user_id,
        workspace_id=workspace_id,
        doc_key=doc_key,
        ops=[{"op": "create", "value": doc_copy}],
        reason=reason,
        base_version=None,
        evidence_refs=provenance or [],
        source="import",
        backend=backend,
    )


async def diff_versions(
    workspace_id: str,
    doc_key: str,
    from_version: int,
    to_version: int,
) -> list[dict[str, Any]]:
    """Return path-level JSON diffs between two version numbers (PK.13)."""
    v_from = await db_knowledge.get_version(workspace_id, doc_key, from_version)
    v_to = await db_knowledge.get_version(workspace_id, doc_key, to_version)
    c_from = v_from["content"] if v_from else None
    c_to = v_to["content"] if v_to else None
    return diff_documents(c_from, c_to)
