"""Publication governance service: check_draft sidecar and publish_report state machine (PK.9–PK.12)."""

from __future__ import annotations

import copy
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import publications as db_publications
from src.dalton.knowledge.paths import PUBLISHED_INDEX, RENDERED
from src.dalton.knowledge.render import render_published_index_md
from src.dalton.knowledge.service import apply_update
from src.dalton.publish.audit import (
    AuditResult,
    aaudit_draft,
    audit_draft,
    render_annotated_draft,
    render_audit_md,
)
from src.dalton.publish.files import (
    compute_bundle_sha256,
    prepend_publication_banner,
    write_published_bundle,
)
from src.dalton.publish.review import ReviewBundle, run_review
from src.dalton.settings import get_dalton_config
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


def _derive_sidecar_paths(draft_path: str) -> tuple[str, str]:
    p = Path(draft_path)
    stem = p.stem
    parent = str(p.parent) if str(p.parent) != "." else "drafts"
    return f"{parent}/{stem}.audit.md", f"{parent}/{stem}.annotated.md"


async def _load_workspace_provenance(
    workspace_id: str,
) -> dict[str, dict[str, Any]]:
    """Load recent provenance records and inline bodies for threads in ``workspace_id``."""
    out: dict[str, dict[str, Any]] = {}
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT pr.provenance_record_id::text,
                           pr.source_type,
                           pr.identifier,
                           pr.title,
                           pr.detail,
                           pr.provider,
                           pr.result_sha256,
                           pr.result_snippet,
                           pb.body_inline
                    FROM provenance_records pr
                    JOIN conversation_threads ct
                      ON ct.conversation_thread_id = pr.conversation_thread_id
                    LEFT JOIN provenance_result_bodies pb
                      ON pb.result_sha256 = pr.result_sha256
                    WHERE ct.workspace_id = %s
                    ORDER BY pr.emitted_at DESC NULLS LAST
                    LIMIT 300
                    """,
                    (workspace_id,),
                )
                for row in await cur.fetchall():
                    rid = str(row[0])
                    out[rid] = {
                        "record_id": rid,
                        "source_type": row[1],
                        "identifier": row[2],
                        "title": row[3],
                        "detail": row[4],
                        "provider": row[5],
                        "result_sha256": row[6],
                        "result_snippet": row[7],
                        "body_inline": row[8],
                    }
    except Exception as e:
        logger.debug("Could not load provenance records for workspace %s: %s", workspace_id, e)
    return out


async def run_check_draft(
    *,
    workspace_id: str,
    draft_path: str,
    facts_path: str | None = None,
    backend: Any,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    use_llm_judge: bool = False,
    user_id: str = "local-dev-user",
    llm_service: Any = None,
) -> dict[str, Any]:
    """Execute ``check_draft``: audit figures, write ``.audit.md`` and ``.annotated.md``, never block (PK.9)."""
    if backend is None:
        return {"ok": False, "message": "ERROR: filesystem backend is unavailable."}

    draft_text = await backend.aread_text(draft_path)
    if draft_text is None:
        return {
            "ok": False,
            "message": f"ERROR: Draft file not found at {draft_path}",
        }

    facts_text: str | None = None
    if facts_path:
        facts_text = await backend.aread_text(facts_path)

    if thesis_doc is None:
        t_row = await db_knowledge.latest_valid_version(workspace_id, "thesis")
        thesis_doc = t_row["content"] if t_row else None
    if models_doc is None:
        m_row = await db_knowledge.latest_valid_version(workspace_id, "models")
        models_doc = m_row["content"] if m_row else None
    if provenance_records is None:
        provenance_records = await _load_workspace_provenance(workspace_id)

    if use_llm_judge:
        audit = await aaudit_draft(
            draft_text,
            thesis_doc=thesis_doc,
            models_doc=models_doc,
            provenance_records=provenance_records,
            facts_markdown=facts_text,
            use_llm_judge=True,
            user_id=user_id,
            llm_service=llm_service,
            workspace_id=workspace_id,
        )
    else:
        audit = audit_draft(
            draft_text,
            thesis_doc=thesis_doc,
            models_doc=models_doc,
            provenance_records=provenance_records,
            facts_markdown=facts_text,
        )

    audit_path, annotated_path = _derive_sidecar_paths(draft_path)
    audit_md = render_audit_md(audit, draft_path=draft_path)
    annotated_md = render_annotated_draft(draft_text, audit)
    await backend.awrite_text(audit_path, audit_md)
    await backend.awrite_text(annotated_path, annotated_md)

    return {
        "ok": True,
        "audit": audit.to_dict(),
        "audit_path": audit_path,
        "annotated_path": annotated_path,
        "message": audit.summary_line(audit_path),
    }


async def _append_agenda_pm_handoff(
    *,
    user_id: str,
    workspace_id: str,
    what: str,
    kind: str,
    backend: Any = None,
) -> None:
    """Append a waiting ``pm_handoffs`` entry to ``agenda.json`` (PK.11 / PK.12)."""
    try:
        ag_row = await db_knowledge.latest_version(workspace_id, "agenda")
        if ag_row is None:
            return
        now_iso = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        await apply_update(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key="agenda",
            ops=[
                {
                    "op": "append",
                    "path": "/pm_handoffs",
                    "value": {
                        "what": what,
                        "kind": kind,
                        "status": "waiting",
                        "since": now_iso,
                        "blocks": [],
                    },
                }
            ],
            reason=f"Await PM {kind}: {what[:80]}",
            base_version=ag_row["version_no"],
            source="server",
            backend=backend,
        )
    except Exception as e:
        logger.warning("Could not append pm_handoff to agenda in %s: %s", workspace_id, e)


async def run_publish(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    tool_call_id: str,
    draft_path: str,
    title: str,
    kind: str,
    slug: str,
    attachments: list[dict[str, Any]] | None = None,
    updates_thesis: bool = False,
    request_id: str | None = None,
    backend: Any,
    author_model: str | None = None,
    origin_type: str | None = None,
    llm_service: Any = None,
    interrupt_fn: Any = None,
) -> dict[str, Any]:
    """Execute the full PK.11 re-entrant publication governance state machine."""
    if backend is None:
        return {"ok": False, "message": "ERROR: filesystem backend is unavailable."}

    # Resolve author_model from active thread usage / thread record when not explicitly provided (G3.2)
    if not author_model:
        author_model = await db_publications.resolve_thread_author_model(thread_id)

    draft_text = await backend.aread_text(draft_path)
    if draft_text is None:
        return {
            "ok": False,
            "message": f"ERROR: Draft file not found at {draft_path}",
        }
    draft_bytes = draft_text.encode("utf-8")

    # Read attachments & compute bundle_sha256
    att_list: list[dict[str, Any]] = []
    att_blobs: list[tuple[str, str, bytes]] = []
    facts_text: str | None = None
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        apath = str(att.get("path") or "")
        arole = str(att.get("role") or "appendix")
        if not apath:
            continue
        raw: bytes | None = None
        if hasattr(backend, "adownload_file_bytes"):
            raw = await backend.adownload_file_bytes(apath)
        if raw is None and hasattr(backend, "aread_text"):
            t = await backend.aread_text(apath)
            if t is not None:
                raw = t.encode("utf-8")
                if arole == "facts":
                    facts_text = t
        elif raw is not None and arole == "facts":
            facts_text = raw.decode("utf-8", errors="replace")

        if raw is not None:
            asha = hashlib.sha256(raw).hexdigest()
            att_list.append({"path": apath, "role": arole, "sha256": asha})
            att_blobs.append((arole, apath, raw))

    bundle_sha256 = compute_bundle_sha256(draft_bytes, att_blobs)

    latest_map = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )
    thesis_row = latest_map.get("thesis")
    thesis_doc = (thesis_row or {}).get("content")
    thesis_version_no = (thesis_row or {}).get("version_no")
    models_doc = (latest_map.get("models") or {}).get("content")
    company_doc = (latest_map.get("company") or {}).get("content") or {}
    ticker = str(company_doc.get("ticker") or slug.split("-")[0]).upper()

    if request_id:
        row = await db_publications.get_request(request_id)
        if not row or str(row["workspace_id"]) != str(workspace_id):
            return {
                "ok": False,
                "message": f"ERROR: publish_request {request_id} not found in this workspace.",
            }
        created = False
    else:
        row, created = await db_publications.get_or_create_request(
            user_id=user_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            kind=kind,
            title=title,
            slug=slug,
            draft_path=draft_path,
            attachments=att_list,
            bundle_sha256=bundle_sha256,
            updates_thesis=updates_thesis,
            thesis_version_no=thesis_version_no if updates_thesis else None,
            status="auditing",
        )

    req_id = str(row["publish_request_id"])
    status = str(row["status"])

    # Fast exits for terminal / already-completed states
    if status == "published":
        pub = (
            await db_publications.get_publication(str(row["publication_id"]))
            if row.get("publication_id")
            else None
        )
        paths = [f["path"] for f in (pub.get("files") or [])] if pub else []
        return {
            "ok": True,
            "status": "published",
            "publication_id": row.get("publication_id"),
            "paths": paths,
            "message": f"Published {ticker} ({title}): {', '.join(paths)} · publication {row.get('publication_id')}",
        }

    if status in ("rejected", "expired", "superseded"):
        return {
            "ok": False,
            "status": status,
            "message": (
                f"Publish request {req_id} is {status}: "
                f"{row.get('decision_message') or row.get('error') or 'please fix and call publish_report again.'}"
            ),
        }

    if status in ("audit_failed", "review_revise", "review_unavailable") and not created:
        if row.get("bundle_sha256") == bundle_sha256:
            return {
                "ok": False,
                "status": status,
                "message": f"Publish request {req_id} remains in {status}; update the draft before retrying.",
            }
        # Bundle changed -> re-enter auditing
        updated = await db_publications.transition(
            req_id,
            (status,),
            "auditing",
            bundle_sha256=bundle_sha256,
            attachments=att_list,
        )
        if updated:
            row = updated
            status = "auditing"

    # Step 2: Figure Audit (when status == 'auditing')
    if status == "auditing":
        prov_records = await _load_workspace_provenance(workspace_id)
        audit = await aaudit_draft(
            draft_text,
            thesis_doc=thesis_doc,
            models_doc=models_doc,
            provenance_records=prov_records,
            facts_markdown=facts_text,
            use_llm_judge=True,
            user_id=user_id,
            llm_service=llm_service,
            workspace_id=workspace_id,
            thread_id=thread_id,
        )
        audit_path, annotated_path = _derive_sidecar_paths(draft_path)
        await backend.awrite_text(audit_path, render_audit_md(audit, draft_path=draft_path))
        await backend.awrite_text(annotated_path, render_annotated_draft(draft_text, audit))

        if not audit.passed:
            await db_publications.transition(
                req_id,
                ("auditing",),
                "audit_failed",
                audit=audit.to_dict(),
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": audit.to_dict(),
                "message": (
                    f"Audit failed ({len(audit.unmatched)} unverified/contradicted figure(s)): "
                    f"see {audit_path}. Fix footnotes/figures and call publish_report again."
                ),
            }

        row = (
            await db_publications.transition(
                req_id,
                ("auditing",),
                "reviewing",
                audit=audit.to_dict(),
            )
            or row
        )
        status = str(row["status"])

    # Step 3: Independent Cross-Family Review (when status == 'reviewing')
    if status == "reviewing":
        thesis_md = await backend.aread_text(RENDERED["thesis"]) or ""
        debate_md = await backend.aread_text(RENDERED["debate"]) or ""
        mgmt_md = await backend.aread_text(RENDERED["management"]) or ""
        rev_res = await run_review(
            user_id=user_id,
            author_model=author_model,
            bundle=ReviewBundle(
                title=title,
                kind=kind,
                draft_markdown=draft_text,
                audit_summary=row.get("audit") or {},
                thesis_md=thesis_md,
                debate_md=debate_md,
                management_md=mgmt_md,
            ),
            llm_service=llm_service,
            workspace_id=workspace_id,
            thread_id=thread_id,
        )
        rev_dict = rev_res.to_dict()
        if rev_res.status == "unavailable":
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "review_unavailable",
                review=rev_dict,
                error=rev_res.reason,
            )
            await _append_agenda_pm_handoff(
                user_id=user_id,
                workspace_id=workspace_id,
                what="配置审稿模型凭证",
                kind="data_access",
                backend=backend,
            )
            return {
                "ok": False,
                "status": "review_unavailable",
                "message": (
                    f"无法发布：缺少独立审稿（{rev_res.reason}）。"
                    f"草稿保留在 {draft_path}，审稿恢复后再调用。"
                ),
            }

        if rev_dict.get("verdict") == "revise":
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "review_revise",
                review=rev_dict,
            )
            fixes = "; ".join(rev_dict.get("must_fix") or [rev_dict.get("summary", "")])
            return {
                "ok": False,
                "status": "review_revise",
                "review": rev_dict,
                "message": (
                    f"Independent review ({rev_dict.get('model')} / {rev_dict.get('family')}) "
                    f"requested revisions: {fixes}"
                ),
            }

        row = (
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "awaiting_pm",
                review=rev_dict,
            )
            or row
        )
        status = str(row["status"])

    # Step 4 & 5: PM Approval (when status == 'awaiting_pm')
    if status == "awaiting_pm":
        if request_id and row.get("bundle_sha256") != bundle_sha256:
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "superseded",
                error="Draft bundle changed while awaiting PM approval",
            )
            return {
                "ok": False,
                "status": "superseded",
                "message": "草稿在审批前已被修改（superseded），请重新调用 publish_report。",
            }

        # 4a. Unattended run -> do NOT emit interrupt(); record pm_handoff and stop at awaiting_pm
        if origin_type in ("automation", "agent"):
            await _append_agenda_pm_handoff(
                user_id=user_id,
                workspace_id=workspace_id,
                what=f"批准正式发布《{title}》（request_id={req_id}）",
                kind="publish_approval",
                backend=backend,
            )
            return {
                "ok": False,
                "status": "awaiting_pm",
                "request_id": req_id,
                "message": f"已通过数字核验与独立审稿，等待 PM 审批，request_id={req_id}",
            }

        # 4b. Interactive thread -> emit ToolApprovalCard interrupt (aligned schema G3.2)
        audit_info = row.get("audit") or {}
        review_info = row.get("review") or {}
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        published_path = f"published/{date_str}-{slug}.md"

        verified_cnt = int(audit_info.get("verified", audit_info.get("matched", 0)))
        contradicted_cnt = int(
            audit_info.get("contradicted", len(audit_info.get("contradicted_items") or []))
        )
        unverified_cnt = int(
            audit_info.get("unverified", len(audit_info.get("unverified_items") or []))
        )
        uncertain_cnt = int(
            audit_info.get("uncertain", len(audit_info.get("uncertain_items") or []))
        )
        exempt_cnt = int(audit_info.get("exempt", 0))
        uncertain_items = list(audit_info.get("uncertain_items") or [])

        labeling_incomplete = bool(audit_info.get("labeling_incomplete", False))

        figure_summary = {
            "verified": verified_cnt,
            "contradicted": contradicted_cnt,
            "unverified": unverified_cnt,
            "uncertain": uncertain_cnt,
            "exempt": exempt_cnt,
            "labeling_incomplete": labeling_incomplete,
            "uncertain_items": uncertain_items,
        }
        reviewer_payload = {
            "model": review_info.get("model", "reviewer"),
            "family": review_info.get("family", ""),
            "verdict": review_info.get("verdict", "pass"),
            "summary": review_info.get("summary", ""),
            "points": review_info.get("points")
            or review_info.get("reasons")
            or review_info.get("must_fix")
            or [],
        }
        audit_summary_str = (
            f"{audit_info.get('figures', 0)} 个数字："
            f"{verified_cnt} 已核实，{contradicted_cnt} 冲突，"
            f"{unverified_cnt} 未核实，{uncertain_cnt} 未能判断，{exempt_cnt} 豁免"
        )
        reviewer_summary_str = (
            f"通过 · {reviewer_payload['model']} （{reviewer_payload['family']}）"
        )
        thesis_ver_str = (
            f"v{thesis_version_no}"
            if updates_thesis and thesis_version_no
            else "不更新"
        )

        payload = {
            "kind": "publish_approval",
            "action_requests": [
                {
                    "type": "publish_report",
                    "name": "publish_report",
                    "tool_call_id": tool_call_id,
                    "attempt_id": req_id,
                    "description": f"正式发布《{title}》",
                    "args": {
                        "request_id": req_id,
                        "ticker": ticker,
                        "title": title,
                        "kind": kind,
                        "draft_path": draft_path,
                        "published_path": published_path,
                        "figure_summary": figure_summary,
                        "labeling_incomplete": labeling_incomplete,
                        "uncertain_items": uncertain_items,
                        "reviewer": reviewer_payload,
                        "updates_thesis": bool(updates_thesis),
                        "thesis_version": thesis_ver_str,
                        "audit_summary": audit_summary_str,
                        "reviewer_summary": reviewer_summary_str,
                        # Preserve Chinese keys for backward compatibility
                        "公司": ticker,
                        "类型": kind,
                        "草稿": draft_path,
                        "数字核验": audit_summary_str,
                        "独立审稿": reviewer_summary_str,
                        "发布为": published_path,
                        "thesis 正式版本": thesis_ver_str,
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": "publish_report",
                    "allowed_decisions": ["approve", "reject"],
                }
            ],
        }
        call_interrupt = interrupt_fn or interrupt
        answer = call_interrupt(payload)
        ans_dict = answer if isinstance(answer, dict) else {}
        decision = (ans_dict.get("order_decisions") or {}).get(req_id) or (
            (ans_dict.get("decisions") or [None])[0]
        )
        dec_type = (
            decision.get("type")
            if isinstance(decision, dict)
            else (str(decision) if decision else None)
        )
        dec_msg = (
            str(decision.get("message") or "")
            if isinstance(decision, dict)
            else ""
        )

        # Verify PM user authorization
        pm_users = set(get_dalton_config().pm_user_ids)
        if user_id not in pm_users:
            return {
                "ok": False,
                "status": "awaiting_pm",
                "message": f"只有 PM（{sorted(pm_users)}）能批准正式发布。",
            }

        now_dt = datetime.now(timezone.utc)
        if dec_type != "approve":
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "rejected",
                decided_by=user_id,
                decided_at=now_dt,
                decision_message=dec_msg or "Rejected by PM",
            )
            return {
                "ok": False,
                "status": "rejected",
                "message": f"PM 拒绝发布：{dec_msg or '无附加说明'}",
            }

        row = (
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "approved",
                decided_by=user_id,
                decided_at=now_dt,
                decision_message=dec_msg,
            )
            or row
        )
        status = str(row["status"])

    # Step 6: Consume approval (approve-once, execute-once)
    if status == "approved":
        claimed = await db_publications.consume_approval(req_id, bundle_sha256)
        if claimed is None:
            await db_publications.transition(
                req_id,
                ("approved",),
                "superseded",
                error="Draft bundle changed after approval",
            )
            return {
                "ok": False,
                "status": "superseded",
                "message": "草稿在审批后被改动，请重新发布（superseded）。",
            }
        row = claimed
        status = "publishing"

    # Step 7, 8, 9: Write immutable files, insert publication rows, update thesis.official
    if status == "publishing":
        now_dt = datetime.now(timezone.utc)
        pub_date_str = now_dt.strftime("%Y-%m-%d")
        approved_at_iso = (
            now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        review_info = row.get("review") or {}
        audit_info = row.get("audit") or {}
        approver = str(row.get("decided_by") or user_id)

        pub_markdown = prepend_publication_banner(
            draft_text,
            title=title,
            kind=kind,
            published_date=pub_date_str,
            approver_user_id=approver,
            approved_at=approved_at_iso,
            reviewer_model=str(review_info.get("model") or "reviewer"),
            reviewer_family=str(review_info.get("family") or "independent"),
            publication_id=req_id,
            thesis_version_no=thesis_version_no if updates_thesis else None,
        )

        files_meta, file_blobs, file_notes = await write_published_bundle(
            slug=slug,
            published_date=pub_date_str,
            published_markdown=pub_markdown,
            attachments=att_list,
            backend=backend,
        )

        pub_row = await db_publications.insert_publication(
            publish_request_id=req_id,
            user_id=user_id,
            workspace_id=workspace_id,
            ticker=ticker,
            kind=kind,
            title=title,
            published_date=pub_date_str,
            files=files_meta,
            file_blobs=file_blobs,
            bundle_sha256=bundle_sha256,
            sources=audit_info.get("matches") or [],
            audit_summary={
                "figures": audit_info.get("figures", 0),
                "matched": audit_info.get("matched", 0),
                "verified": audit_info.get("verified", audit_info.get("matched", 0)),
                "contradicted": audit_info.get("contradicted", 0),
                "unverified": audit_info.get("unverified", 0),
                "uncertain": audit_info.get("uncertain", 0),
                "exempt": audit_info.get("exempt", 0),
            },
            reviewer={
                "model": review_info.get("model"),
                "family": review_info.get("family"),
                "verdict": review_info.get("verdict", "pass"),
                "reasons": review_info.get("reasons") or [],
                "reviewed_at": review_info.get("reviewed_at") or approved_at_iso,
            },
            approver_user_id=approver,
            approved_at=now_dt,
            thesis_version_no=thesis_version_no if updates_thesis else None,
        )
        pub_id = str(pub_row["publication_id"])

        # Step 9: Update thesis.official when updates_thesis=True (source='publish')
        official_ver_note = ""
        thesis_update_error: str | None = None
        if updates_thesis and thesis_row is not None and isinstance(thesis_doc, dict):
            frozen_pillars = copy.deepcopy(thesis_doc.get("pillars") or [])
            ops_for_official: list[dict[str, Any]] = [
                {
                    "op": "set",
                    "path": "/official",
                    "value": {
                        "version_no": int(thesis_row["version_no"]),
                        "publication_id": pub_id,
                        "approved_by": approver,
                        "approved_at": approved_at_iso,
                        "house_view": copy.deepcopy(thesis_doc["house_view"]),
                        "aggregate_status": thesis_doc["aggregate_status"],
                        "pillars": frozen_pillars,
                    },
                }
            ]
            for p in thesis_doc.get("pillars") or []:
                if isinstance(p, dict) and p.get("id"):
                    ops_for_official.append(
                        {
                            "op": "set",
                            "path": f"/pillars/{p['id']}/provenance",
                            "value": "approved",
                        }
                    )
            off_res = await apply_update(
                user_id=user_id,
                workspace_id=workspace_id,
                doc_key="thesis",
                ops=ops_for_official,
                reason=f"Official publication {pub_id}: {title}",
                base_version=int(thesis_row["version_no"]),
                source="publish",
                backend=backend,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
            )
            if off_res.get("ok"):
                official_ver_note = f" · thesis official v{off_res['version_no']}"
            else:
                thesis_update_error = str(
                    off_res.get("message")
                    or off_res.get("errors")
                    or "thesis.official update failed"
                )
                file_notes.append(f"thesis.official 更新失败：{thesis_update_error}")
                try:
                    await db_publications.transition(
                        req_id,
                        ("published",),
                        "published",
                        error=f"thesis.official update failed: {thesis_update_error}",
                    )
                except Exception as err_trans:
                    logger.warning(
                        "Failed to record thesis.official error on publish_request %s: %s",
                        req_id,
                        err_trans,
                    )

        # Render published/INDEX.md
        all_pubs = await db_publications.list_publications(workspace_id)
        index_md = render_published_index_md(all_pubs, ticker=ticker)
        await backend.awrite_text(PUBLISHED_INDEX, index_md)

        paths_str = ", ".join(f["path"] for f in files_meta)
        notes_str = f" ({'; '.join(file_notes)})" if file_notes else ""
        res_out: dict[str, Any] = {
            "ok": True,
            "status": "published",
            "publication_id": pub_id,
            "paths": [f["path"] for f in files_meta],
            "message": (
                f"Published {ticker} {title}: {paths_str}"
                f"{official_ver_note} · publication {pub_id[:8]}{notes_str}"
            ),
        }
        if thesis_update_error:
            res_out["thesis_update_error"] = thesis_update_error
        return res_out

    return {
        "ok": False,
        "status": status,
        "message": f"Unexpected publish request status: {status}",
    }

