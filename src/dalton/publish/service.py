"""Publication governance service: check_draft sidecar and publish_report state machine (PK.9–PK.12)."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from langgraph.types import interrupt

from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import publications as db_publications
from src.dalton.heavy_compute import (
    HeavyComputeTimeout,
    run_in_heavy_subprocess,
)
from src.dalton.knowledge.paths import PUBLISHED_INDEX, RENDERED
from src.dalton.knowledge.render import render_published_index_md
from src.dalton.knowledge.service import apply_update
from src.dalton.publish.audit import (
    AuditBuildTimeout,
    aaudit_draft,
    resolve_audit_config_fingerprint,
    compute_audit_inputs_sha256,
    format_audit_summary_from_dict,
    render_annotated_draft,
    render_audit_md,
    try_load_reusable_audit_from_md,
)
from src.dalton.publish.files import (
    compute_bundle_sha256,
    prepend_publication_banner,
    write_published_bundle,
)
from src.dalton.publish.match import (
    SourceParseCache,
    _extract_evidence_ref_path,
    _extract_segment_qualifier,
    _is_draft_provenance_record,
    _is_draft_self_ref,
    _is_legacy_thesis_evidence,
    _normalize_cell_coord,
    _normalize_model_spec,
    count_unrecalculated_formulas,
    metrics_match,
    read_model_cell,
    values_within_tolerance,
)
from src.dalton.publish.numbers import (
    extract_figures,
    normalize_period,
    parse_facts_ledger,
    parse_footnotes,
)
from src.dalton.publish.review import ReviewBundle, run_review
from src.dalton.settings import get_dalton_config
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_PROV_REF_RE = re.compile(r"\bprov:([0-9a-fA-F-]+)\b")
_MODEL_REF_RE = re.compile(
    r"\bmodel:([^\s,;)}\]>#!]+\.xlsx[#!]![^\s,;)}\]>]+|[^\s,;)}\]>#!]+\.xlsx[#!][^\s,;)}\]>]+!\$?[A-Za-z]{1,3}\$?\d+)\b",
    re.IGNORECASE,
)


def _derive_sidecar_paths(draft_path: str) -> tuple[str, str]:
    p = Path(draft_path)
    stem = p.stem
    parent = str(p.parent) if str(p.parent) != "." else "drafts"
    return f"{parent}/{stem}.audit.md", f"{parent}/{stem}.annotated.md"


async def _read_optional_text(backend: Any, path: str) -> str | None:
    if hasattr(backend, "aread_text"):
        try:
            val = await backend.aread_text(path)
            return val if isinstance(val, str) else None
        except Exception:
            return None
    if hasattr(backend, "adownload_file_bytes"):
        try:
            raw = await backend.adownload_file_bytes(path)
            return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else None
        except Exception:
            return None
    return None


async def _read_optional_bytes(backend: Any, path: str) -> bytes | None:
    if hasattr(backend, "adownload_file_bytes"):
        try:
            raw = await backend.adownload_file_bytes(path)
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)
        except Exception:
            pass
    if hasattr(backend, "aread_bytes"):
        try:
            raw = await backend.aread_bytes(path)
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)
        except Exception:
            pass
    return None


def _extract_referenced_prov_ids(
    draft_text: str, facts_text: str | None = None
) -> set[str]:
    refs: set[str] = set()
    for text in (draft_text, facts_text or ""):
        if not text:
            continue
        for m in _PROV_REF_RE.finditer(text):
            pid = m.group(1).strip()
            if pid:
                refs.add(pid)
    return refs


def _extract_referenced_model_specs(
    draft_text: str, facts_text: str | None = None
) -> list[str]:
    specs: list[str] = []
    seen: set[str] = set()

    def _add_spec(raw_target: str) -> None:
        t = raw_target.strip()
        if not t.startswith("model:"):
            return
        m_spec = t.split(":", 1)[1].strip()
        if m_spec and m_spec not in seen:
            seen.add(m_spec)
            specs.append(m_spec)

    if draft_text:
        for raw_bundle in parse_footnotes(draft_text).values():
            for t in raw_bundle.split(";"):
                _add_spec(t)
        for m in _MODEL_REF_RE.finditer(draft_text):
            _add_spec(f"model:{m.group(1)}")

    if facts_text:
        for row in parse_facts_ledger(facts_text).values():
            for t in str(row.get("ref") or "").split(";"):
                _add_spec(t)
        for m in _MODEL_REF_RE.finditer(facts_text):
            _add_spec(f"model:{m.group(1)}")

    return specs


async def _load_referenced_model_cells(
    backend: Any,
    draft_text: str,
    facts_text: str | None = None,
    models_doc: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read `.xlsx` cells explicitly referenced by `model:<path>#<Sheet>!<Cell>` in the draft or facts ledger."""
    specs = _extract_referenced_model_specs(draft_text, facts_text)
    if not specs or backend is None:
        return {}

    version_meta: dict[str, tuple[bool, str]] = {}
    for ver in (models_doc or {}).get("versions") or []:
        if isinstance(ver, dict) and ver.get("path"):
            vpath = str(ver["path"]).strip()
            version_meta[vpath] = (
                bool(ver.get("recalculated", True)),
                str(ver.get("sha256") or ""),
            )

    xlsx_cache: dict[str, bytes | None] = {}
    cells_out: dict[str, dict[str, Any]] = {}

    for m_spec in specs:
        if "#" in m_spec:
            xlsx_path, sheet_cell = m_spec.split("#", 1)
        elif "!" in m_spec:
            xlsx_path, sheet_cell = m_spec.split("!", 1)
        else:
            continue
        xlsx_path = xlsx_path.strip()
        sheet_cell = sheet_cell.strip()
        if not xlsx_path or not sheet_cell:
            continue
        if "!" in sheet_cell:
            sheet_name, cell_coord = sheet_cell.split("!", 1)
        else:
            sheet_name, cell_coord = "", sheet_cell
        norm_coord = _normalize_cell_coord(cell_coord)
        norm_sheet_cell = (
            f"{sheet_name.strip()}!{norm_coord}" if sheet_name.strip() else norm_coord
        )
        if xlsx_path not in xlsx_cache:
            xlsx_cache[xlsx_path] = await _read_optional_bytes(backend, xlsx_path)
        xlsx_bytes = xlsx_cache[xlsx_path]
        recalc, _sha256_meta = version_meta.get(xlsx_path, (True, ""))
        if not xlsx_bytes:
            dl_err: dict[str, Any] = {
                "value": None,
                "unit": "",
                "period": "",
                "metric": "",
                "recalculated": recalc,
                "sha256": "",
                "file_sha256": "",
                "from_xlsx": True,
                "error": f"模型文件 {xlsx_path} 读取失败或内容为空",
            }
            cells_out[f"{xlsx_path}#{sheet_cell}"] = dl_err
            cells_out[f"{xlsx_path}#{norm_sheet_cell}"] = dl_err
            cells_out[m_spec] = dl_err
            cells_out[_normalize_model_spec(m_spec)] = dl_err
            cells_out[xlsx_path] = dl_err
            continue
        cell_read = await asyncio.to_thread(
            read_model_cell, xlsx_bytes, sheet_name, cell_coord
        )
        val, unit_hint, err = cell_read
        file_sha = hashlib.sha256(xlsx_bytes).hexdigest()
        if (
            err is not None
            or val is None
            or isinstance(val, bool)
            or not isinstance(val, (int, float))
        ):
            if err is not None:
                err_msg = err
            elif val is None:
                err_msg = f"模型单元格 {m_spec} 读取失败"
            else:
                err_msg = f"模型单元格 {norm_sheet_cell} 非数值 ({val!r})"
            err_entry: dict[str, Any] = {
                "value": None,
                "unit": "",
                "period": "",
                "metric": "",
                "recalculated": recalc,
                "sha256": file_sha,
                "file_sha256": file_sha,
                "from_xlsx": True,
                "error": err_msg,
            }
            cells_out[f"{xlsx_path}#{sheet_cell}"] = err_entry
            cells_out[f"{xlsx_path}#{norm_sheet_cell}"] = err_entry
            cells_out[m_spec] = err_entry
            cells_out[_normalize_model_spec(m_spec)] = err_entry
            if "读取失败" in err_msg or "没有可用工作表" in err_msg or "模型文件为空" in err_msg:
                cells_out[xlsx_path] = err_entry
            continue
        stored_as_ratio = bool(
            getattr(cell_read, "stored_as_ratio", False)
            or getattr(unit_hint, "stored_as_ratio", False)
            or getattr(unit_hint, "is_pct_number_format", False)
        )
        unit_conflict_warning = getattr(cell_read, "unit_conflict_warning", None)
        eff_unit = "pct" if stored_as_ratio else (unit_hint if unit_hint else "")
        cell_currency = None if stored_as_ratio else getattr(unit_hint, "currency", None)
        entry = {
            "value": float(val),
            "unit": eff_unit,
            "currency": cell_currency,
            "stored_as_ratio": stored_as_ratio,
            "pct_from_number_format": stored_as_ratio,
            "unit_conflict_warning": unit_conflict_warning,
            "period": "",
            "metric": "",
            "recalculated": recalc,
            "sha256": file_sha,
            "file_sha256": file_sha,
            "from_xlsx": True,
        }
        cells_out[f"{xlsx_path}#{sheet_cell}"] = entry
        cells_out[f"{xlsx_path}#{norm_sheet_cell}"] = entry
        cells_out[m_spec] = entry
        cells_out[_normalize_model_spec(m_spec)] = entry

    return cells_out


def _collect_referenced_xlsx_paths(
    draft_text: str,
    facts_text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    *,
    include_attachments: bool = True,
) -> list[str]:
    xlsx_paths: list[str] = []
    seen_paths: set[str] = set()

    def _add_path(raw_path: str) -> None:
        p = raw_path.strip()
        if p and p.lower().endswith(".xlsx") and p not in seen_paths:
            seen_paths.add(p)
            xlsx_paths.append(p)

    for m_spec in _extract_referenced_model_specs(draft_text, facts_text):
        if "#" in m_spec:
            _add_path(m_spec.split("#", 1)[0])
        elif "!" in m_spec:
            _add_path(m_spec.split("!", 1)[0])
        else:
            _add_path(m_spec)

    if include_attachments:
        for att in attachments or []:
            if isinstance(att, dict) and str(att.get("role") or "") == "model":
                _add_path(str(att.get("path") or ""))
    return xlsx_paths


async def _check_unrecalculated_models(
    backend: Any,
    draft_text: str,
    facts_text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    att_blobs: list[tuple[str, str, bytes]] | None = None,
) -> list[str]:
    """Check referenced ``model:`` ``.xlsx`` files and ``role="model"`` ``.xlsx`` attachments for unrecalculated formulas or parse errors."""
    if backend is None:
        return []
    xlsx_paths = _collect_referenced_xlsx_paths(
        draft_text, facts_text, attachments, include_attachments=True
    )

    blob_map: dict[str, bytes] = {
        apath: raw for _arole, apath, raw in (att_blobs or []) if apath and raw
    }
    warnings_out: list[str] = []
    for xp in xlsx_paths:
        raw_bytes = blob_map.get(xp)
        if raw_bytes is None:
            raw_bytes = await _read_optional_bytes(backend, xp)
        if not raw_bytes:
            warnings_out.append(
                f"模型文件无法读取或为空：{xp}，请检查模型文件并运行 `python .agents/skills/xlsx/scripts/recalc.py {xp}` 后再发布"
            )
            continue
        try:
            missing_cnt = await asyncio.to_thread(
                count_unrecalculated_formulas, raw_bytes
            )
        except Exception as e:
            warnings_out.append(
                f"模型文件损坏或解析失败（{xp}：{e}），请修复并运行 `python .agents/skills/xlsx/scripts/recalc.py {xp}` 后再发布"
            )
            continue
        if missing_cnt > 0:
            warnings_out.append(
                f"模型未重算：{missing_cnt} 个公式没有缓存值，请运行 `python .agents/skills/xlsx/scripts/recalc.py {xp}` 后再发布"
            )
    return warnings_out


async def _verify_referenced_model_sha256s(
    backend: Any,
    draft_text: str,
    facts_text: str | None,
    audit_dict: dict[str, Any] | None,
) -> str | None:
    """Verify that every ``model:`` ``.xlsx`` file referenced in the draft/facts still matches its audit-time ``sha256``."""
    if backend is None:
        return None
    ref_paths = _collect_referenced_xlsx_paths(
        draft_text, facts_text, attachments=None, include_attachments=False
    )
    audit_info = audit_dict if isinstance(audit_dict, dict) else {}
    recorded_map: dict[str, str] = {}
    for k, v in (audit_info.get("model_file_sha256s") or {}).items():
        if k and v:
            recorded_map[str(k).strip().lstrip("./")] = str(v).strip()
    for m in audit_info.get("matches") or []:
        if not isinstance(m, dict) or m.get("kind") != "model":
            continue
        m_sha = str(m.get("source_sha256") or "").strip()
        m_ref = str(m.get("ref") or "").strip()
        if m_sha and m_ref:
            base_p = (
                m_ref.split("#", 1)[0]
                if "#" in m_ref
                else (m_ref.split("!", 1)[0] if "!" in m_ref else m_ref)
            ).strip().lstrip("./")
            if base_p and base_p.lower().endswith(".xlsx"):
                recorded_map.setdefault(base_p, m_sha)
                if base_p not in ref_paths:
                    ref_paths.append(base_p)

    for xp in ref_paths:
        norm_xp = xp.strip().lstrip("./")
        expected_sha = recorded_map.get(norm_xp) or recorded_map.get(xp)
        raw_bytes = await _read_optional_bytes(backend, xp)
        if not raw_bytes:
            return f"引用的模型文件 {xp} 无法读取或已被删除，请重新运行数字核验后再发布。"
        curr_sha = hashlib.sha256(raw_bytes).hexdigest()
        if not expected_sha or curr_sha != expected_sha:
            return f"引用的模型文件 {xp} 在核验后已被修改（sha256 不一致），请重新核验后再发布。"
    return None


async def _load_evidence_ref_bodies(
    backend: Any,
    thesis_doc: dict[str, Any] | None,
    draft_path: str = "",
) -> dict[str, str]:
    """Load workspace file bodies referenced by `thesis.json` `evidence[].source.ref`, excluding the draft itself."""
    if backend is None or not isinstance(thesis_doc, dict):
        return {}
    out: dict[str, str] = {}
    for ev in thesis_doc.get("evidence") or []:
        if not isinstance(ev, dict) or _is_legacy_thesis_evidence(ev):
            continue
        ref_path = _extract_evidence_ref_path(ev)
        if not ref_path or _is_draft_self_ref(ref_path, draft_path):
            continue
        if ref_path in out:
            continue
        text = await _read_optional_text(backend, ref_path)
        if isinstance(text, str) and text.strip():
            out[ref_path] = text
    return out


def _prov_hits_all_key_figures(
    draft_text: str,
    prov_map: dict[str, dict[str, Any]],
    *,
    draft_path: str = "",
) -> bool:
    """Return True if every non-exempt figure in ``draft_text`` (not already pointing to ``evidence:``/``model:``) hits in ``prov_map`` with matching metric, period, value, and unit."""
    if not draft_text or not draft_text.strip():
        return True
    figs = [
        f
        for f in extract_figures(draft_text)
        if not f.exempt
        and f.label_hint != "incidental"
        and not (f.footnote_target or "").startswith(("evidence:", "model:"))
    ]
    if not figs:
        return True
    if not prov_map:
        return False
    cache = SourceParseCache()
    for fig in figs:
        fn_t = (fig.footnote_target or "").strip()
        target_pid = ""
        if fn_t.startswith("prov:"):
            target_pid = fn_t.split(":", 1)[1].strip()
            if target_pid not in prov_map and not any(
                k.startswith(target_pid) for k in prov_map
            ):
                return False
        fig_p = normalize_period(fig.period)
        hit_this_fig = False
        for pid, prec in prov_map.items():
            if target_pid and pid != target_pid and not pid.startswith(target_pid):
                continue
            if _is_draft_provenance_record(prec, draft_path):
                continue
            body = str(
                prec.get("body")
                or prec.get("body_inline")
                or prec.get("result_snippet")
                or ""
            )
            if not body:
                continue
            all_body_periods = cache.periods(body)
            for sf in cache.figures(body):
                if sf.exempt:
                    continue
                if fig.metric:
                    if not sf.metric or not metrics_match(fig.metric, sf.metric):
                        continue
                    seg1 = _extract_segment_qualifier(fig.metric)
                    seg2 = _extract_segment_qualifier(sf.metric)
                    if seg1 and seg2 and seg1 != seg2:
                        continue
                sf_p = normalize_period(sf.period)
                if fig_p:
                    if sf_p and sf_p != fig_p:
                        continue
                    if not sf_p and (
                        not all_body_periods or fig_p not in all_body_periods
                    ):
                        continue
                ok_v, _, _ = values_within_tolerance(fig, sf.value, sf.unit, sf.scale)
                if ok_v:
                    hit_this_fig = True
                    break
            if hit_this_fig:
                break
        if not hit_this_fig:
            return False
    return True


async def _load_workspace_provenance(
    workspace_id: str,
    *,
    draft_path: str = "",
    draft_text: str = "",
    referenced_ids: set[str] | None = None,
    initial_limit: int = 300,
    max_limit: int = 1500,
    deadline: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Load provenance records and inline bodies for threads in ``workspace_id`` using tiered + on-demand retrieval.

    ``deadline`` (``time.monotonic()``) covers the source precheck too: the
    figures × sources cross-match used to run straight on the event loop, with
    no yield and no budget, which is a confirmed cause of the 60s+ stalls (A1).
    A missing deadline defaults to the configured audit budget.
    """
    if deadline is None:
        deadline = time.monotonic() + get_dalton_config().publish.audit_total_timeout_s
    out: dict[str, dict[str, Any]] = {}

    def _add_row(row: Any) -> None:
        if len(out) >= max_limit:
            return
        rid = str(row[0])
        if rid in out:
            return
        rec = {
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
        if _is_draft_provenance_record(rec, draft_path):
            return
        out[rid] = rec

    try:
        first_limit = max(0, min(initial_limit, max_limit))
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                if first_limit > 0:
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
                        LIMIT %s
                        """,
                        (workspace_id, first_limit),
                    )
                    for row in await cur.fetchall():
                        _add_row(row)

                missing_refs = [
                    r
                    for r in sorted(referenced_ids or set())
                    if r not in out and not any(k.startswith(r) for k in out)
                ]
                remaining_for_refs = max(0, max_limit - len(out))
                if missing_refs and remaining_for_refs > 0:
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
                          AND pr.provenance_record_id::text = ANY(%s)
                        LIMIT %s
                        """,
                        (workspace_id, missing_refs[:remaining_for_refs], remaining_for_refs),
                    )
                    for row in await cur.fetchall():
                        _add_row(row)

                remaining_for_expand = max(0, max_limit - len(out))
                if remaining_for_expand > 0 and max_limit > first_limit and bool(
                    draft_text and draft_text.strip()
                ):
                    # A1/A5: the precheck runs in a killable subprocess under the
                    # audit-wide deadline. On a budget overrun, load the wider tier
                    # anyway: the match phase re-checks against it under the same
                    # deadline and marks what it cannot finish.
                    try:
                        hits = await run_in_heavy_subprocess(
                            _prov_hits_all_key_figures,
                            draft_text,
                            out,
                            draft_path=draft_path,
                            deadline=deadline,
                            label="publish.prov_precheck",
                        )
                    except HeavyComputeTimeout:
                        logger.warning(
                            "Provenance precheck for %s hit the audit deadline; "
                            "loading the wider provenance tier regardless",
                            workspace_id,
                        )
                        hits = False
                    if not hits:
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
                            LIMIT %s OFFSET %s
                            """,
                            (workspace_id, remaining_for_expand, first_limit),
                        )
                        for row in await cur.fetchall():
                            _add_row(row)
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
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    evidence_ref_bodies: dict[str, str] | None = None,
    use_llm_judge: bool = True,
    user_id: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Execute ``check_draft``: audit figures via ``aaudit_draft``, write ``.audit.md`` and ``.annotated.md``, never block (PK.9 / P0I §1.7)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
    if backend is None:
        return {"ok": False, "message": "ERROR: filesystem backend is unavailable."}

    draft_text = await backend.aread_text(draft_path)
    if draft_text is None:
        return {
            "ok": False,
            "message": f"ERROR: Draft file not found at {draft_path}",
        }

    # A5: one wall-clock budget for the whole check, starting HERE — before the
    # provenance precheck and the reusable-audit hash, which previously ran
    # before the deadline existed and so could burn unbounded CPU (and loop
    # time) before the budgeted phases even began.
    audit_deadline = time.monotonic() + get_dalton_config().publish.audit_total_timeout_s

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
        provenance_records = await _load_workspace_provenance(
            workspace_id,
            draft_path=draft_path,
            draft_text=draft_text,
            referenced_ids=_extract_referenced_prov_ids(draft_text, facts_text),
            deadline=audit_deadline,
        )
    if model_cells is None:
        model_cells = await _load_referenced_model_cells(
            backend, draft_text, facts_text, models_doc
        )
    if evidence_ref_bodies is None:
        evidence_ref_bodies = await _load_evidence_ref_bodies(
            backend, thesis_doc, draft_path=draft_path
        )

    unrecalc_warnings = await _check_unrecalculated_models(
        backend, draft_text, facts_text
    )
    audit_path, annotated_path = _derive_sidecar_paths(draft_path)
    existing_audit_md = await _read_optional_text(backend, audit_path)
    # A5: the reusable-audit hash re-serialises every input (all provenance
    # bodies included), so it rides the same deadline and subprocess as the
    # phases it stands before. A budget overrun just means no cached reuse:
    # the audit below runs under the same deadline and degrades the same way.
    try:
        cached_audit = await run_in_heavy_subprocess(
            try_load_reusable_audit_from_md,
            existing_audit_md,
            draft_markdown=draft_text,
            thesis_doc=thesis_doc,
            models_doc=models_doc,
            model_cells=model_cells,
            provenance_records=provenance_records,
            facts_markdown=facts_text,
            evidence_ref_bodies=evidence_ref_bodies,
            use_llm_judge=use_llm_judge,
            config_fingerprint=await resolve_audit_config_fingerprint(
                user_id, use_llm_judge=use_llm_judge
            ),
            deadline=audit_deadline,
            label="publish.audit_cache_check",
        )
    except HeavyComputeTimeout:
        logger.warning(
            "Reusable-audit hash for %s hit the audit deadline; re-auditing",
            draft_path,
        )
        cached_audit = None
    if cached_audit is not None:
        cached_audit.unrecalculated_model_warnings = unrecalc_warnings
        if unrecalc_warnings and any(
            w not in (existing_audit_md or "") for w in unrecalc_warnings
        ):
            await backend.awrite_text(
                audit_path,
                await asyncio.to_thread(
                    render_audit_md, cached_audit, draft_path=draft_path
                ),
            )
        warn_lead = f"{'；'.join(unrecalc_warnings)}。" if unrecalc_warnings else ""
        return {
            "ok": True,
            "reused": True,
            "audit": cached_audit.to_dict(),
            "audit_path": audit_path,
            "annotated_path": annotated_path,
            "message": f"{warn_lead}{cached_audit.summary_line(audit_path)}",
        }

    try:
        audit = await aaudit_draft(
            draft_text,
            thesis_doc=thesis_doc,
            models_doc=models_doc,
            model_cells=model_cells,
            provenance_records=provenance_records,
            facts_markdown=facts_text,
            evidence_ref_bodies=evidence_ref_bodies,
            draft_path=draft_path,
            use_llm_judge=use_llm_judge,
            user_id=user_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            deadline=audit_deadline,
        )
    except AuditBuildTimeout as e:
        # The assembly phase died at its grace budget: no sidecars to write,
        # the tool result itself reports the failure.
        return {"ok": False, "message": str(e)}
    audit.unrecalculated_model_warnings = unrecalc_warnings

    audit_md = await asyncio.to_thread(render_audit_md, audit, draft_path=draft_path)
    annotated_md = await asyncio.to_thread(render_annotated_draft, draft_text, audit)
    await backend.awrite_text(audit_path, audit_md)
    await backend.awrite_text(annotated_path, annotated_md)

    warn_lead = f"{'；'.join(unrecalc_warnings)}。" if unrecalc_warnings else ""
    return {
        "ok": True,
        "reused": False,
        "audit": audit.to_dict(),
        "audit_path": audit_path,
        "annotated_path": annotated_path,
        "message": f"{warn_lead}{audit.summary_line(audit_path)}",
    }


def _prefix_with_audit_summary(row: dict[str, Any], message: str) -> str:
    """Prefix ``message`` with the unified 4-bucket figure audit summary if ``row['audit']`` is present."""
    audit_dict = row.get("audit") if isinstance(row.get("audit"), dict) else None
    summary = format_audit_summary_from_dict(audit_dict)
    return f"{summary}。{message}" if summary else message


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


# Round-3 decision 4: the initiating thread may be gone; the card/handoff then
# say so instead of showing an empty initiator.
PUBLISH_INITIATOR_UNKNOWN = "发起人未知（原线程已删除）"


async def _resolve_publish_initiator(
    row: dict[str, Any],
    ws_owner_id: str,
    *,
    caller_thread_id: str,
    caller_user_id: str,
) -> str | None:
    """The initiator of a publish request, for re-entry authorization and for
    naming on the approval card / agenda handoff.

    Round-4 decision 4: a first filing (or any call whose thread IS the row's
    ``conversation_thread_id``) is judged by the current caller. Only a
    CROSS-THREAD request_id re-entry looks the row's thread up: its owner is
    the initiator, and a thread that can no longer be read surfaces as
    ``PUBLISH_INITIATOR_UNKNOWN`` instead of widening re-entry."""
    row_thread_id = str(row.get("conversation_thread_id") or "").strip()
    caller_uid = str(caller_user_id or "").strip()

    def _by_caller() -> str | None:
        return caller_uid if caller_uid and caller_uid != ws_owner_id else None

    if not row_thread_id or row_thread_id == str(caller_thread_id or "").strip():
        return _by_caller()
    try:
        from src.server.database import conversation as conv_db

        thread = await conv_db.get_thread_by_id(row_thread_id)
        thread_owner = str((thread or {}).get("user_id") or "").strip()
        if thread_owner:
            return thread_owner if thread_owner != ws_owner_id else None
        return PUBLISH_INITIATOR_UNKNOWN
    except Exception as exc:
        logger.debug(
            "Could not resolve initiator thread %s for publish request: %s",
            row_thread_id,
            exc,
        )
        return PUBLISH_INITIATOR_UNKNOWN


# Round-3 decision 1: when a caller re-enters by request_id, the request row's
# saved params win and the caller's differing ones are only reported back.
# The note travels to the wrapper through a ContextVar so every exit path of
# the state machine can carry it without threading it through each return.
_IGNORED_PARAMS_NOTE: contextvars.ContextVar[list[str] | None] = (
    contextvars.ContextVar("_publish_ignored_params", default=None)
)


def _adopt_request_params(
    row: dict[str, Any],
    *,
    title: str,
    kind: str,
    slug: str,
    draft_path: str,
    updates_thesis: bool,
    attachments: list[dict[str, Any]] | None,
) -> tuple[str, str, str, str, bool, list[dict[str, Any]] | None]:
    """Round-3 decision 1: on request_id re-entry the row's saved
    ``title/kind/slug/draft_path/updates_thesis/attachments`` are authoritative.
    Returns the adopted values and records which caller-supplied params
    differed, for the wrapper to name in the result message."""
    ignored: list[str] = []
    row_title = str(row.get("title") or "")
    row_kind = str(row.get("kind") or "")
    row_slug = str(row.get("slug") or "")
    row_draft = str(row.get("draft_path") or "")
    row_updates = bool(row.get("updates_thesis"))

    if row_title and row_title != title:
        ignored.append("title")
    if row_kind and row_kind != kind:
        ignored.append("kind")
    if row_slug and row_slug != slug:
        ignored.append("slug")
    if row_draft and row_draft != draft_path:
        ignored.append("draft_path")
    if row_updates != bool(updates_thesis):
        ignored.append("updates_thesis")
    row_atts = row.get("attachments") if isinstance(row.get("attachments"), list) else None
    if row_atts is not None:
        caller_atts = [
            {"path": str(a.get("path") or ""), "role": str(a.get("role") or "appendix")}
            for a in (attachments or [])
            if isinstance(a, dict)
        ]
        row_atts_meta = [
            {"path": str(a.get("path") or ""), "role": str(a.get("role") or "appendix")}
            for a in row_atts
            if isinstance(a, dict)
        ]
        if row_atts_meta != caller_atts:
            ignored.append("attachments")
    if ignored:
        _IGNORED_PARAMS_NOTE.set(ignored)

    return (
        row_title or title,
        row_kind or kind,
        row_slug or slug,
        row_draft or draft_path,
        row_updates,
        row_atts if row_atts is not None else attachments,
    )


def _thesis_version_drift(
    row: dict[str, Any],
    thesis_row: dict[str, Any] | None,
) -> str | None:
    """Round-3/4 decision: a request that updates thesis is only executable
    while the workspace's thesis is still what the request was filed against.

    An empty pinned ``thesis_version_no`` means the workspace had NO thesis
    when the request was filed (round-4 decision 2): a thesis existing by
    execution time is a drift, not a skip."""
    if not bool(row.get("updates_thesis")):
        return None
    approved_ver = row.get("thesis_version_no")
    if not approved_ver:
        if thesis_row is not None:
            return "请求发起时工作区尚无 thesis，而现在已存在（thesis_version_no 缺失）"
        return None
    current_ver = (thesis_row or {}).get("version_no")
    if current_ver is None or int(current_ver) != int(approved_ver):
        return "thesis 版本与送审时不一致（thesis 在送审后已被修改）"
    return None


async def _run_publish_state_machine(
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
    interrupt_fn: Any = None,
) -> dict[str, Any]:
    """Execute the full PK.11 re-entrant publication governance state machine."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    ws_row = await verify_workspace_access(
        workspace_id, user_id, need="write", channel="cos"
    )
    # A colleague with workspace write access may run the publish state machine,
    # but every ownership field it writes (request row, publication row, agenda
    # handoff, thesis.official) belongs to the workspace owner; `user_id` below
    # stays the caller for model-config resolution and billing, which fall back
    # user -> team credential holder -> yaml.
    ws_owner_id = str((ws_row or {}).get("user_id") or "").strip() or user_id
    if backend is None:
        return {"ok": False, "message": "ERROR: filesystem backend is unavailable."}

    # Round-3 decision 1: with request_id the row is authoritative — resolve it
    # BEFORE reading anything so the row's draft_path/attachments/title/…
    # drive the whole run; the caller's differing params are only reported.
    row: dict[str, Any] | None = None
    created = False
    if request_id:
        row = await db_publications.get_request(request_id)
        if not row or str(row["workspace_id"]) != str(workspace_id):
            return {
                "ok": False,
                "message": f"ERROR: publish_request {request_id} not found in this workspace.",
            }
        # Round-2 decision D: re-entry by request_id is the workspace owner's
        # or the original initiator's (resolved from the request row's thread,
        # decision C) — any other colleague gets 403 even with workspace write.
        initiator_id = await _resolve_publish_initiator(
            row,
            ws_owner_id,
            caller_thread_id=thread_id,
            caller_user_id=user_id,
        )
        row_owner = str(row.get("user_id") or "").strip()
        allowed_reentry = {ws_owner_id}
        if initiator_id and initiator_id != PUBLISH_INITIATOR_UNKNOWN:
            allowed_reentry.add(initiator_id)
        if not row_owner or str(user_id) not in allowed_reentry:
            raise HTTPException(
                status_code=403,
                detail="Publication request does not belong to current user",
            )
        created = False
        req_id = str(row["publish_request_id"])
        status = str(row["status"])
        # Terminal states need no draft read — the draft may already be gone.
        if status in ("rejected", "expired", "superseded"):
            return {
                "ok": False,
                "status": status,
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"Publish request {req_id} is {status}: "
                    f"{row.get('decision_message') or row.get('error') or 'please fix and call publish_report again.'}",
                ),
            }
        (
            title,
            kind,
            slug,
            draft_path,
            updates_thesis,
            attachments,
        ) = _adopt_request_params(
            row,
            title=title,
            kind=kind,
            slug=slug,
            draft_path=draft_path,
            updates_thesis=updates_thesis,
            attachments=attachments,
        )
        # The approval's pinned thesis version travels on the row (round-3
        # decision 1): execution reads it from there, not from this call.
        approved_thesis_version_no = row.get("thesis_version_no")

    # Resolve author_model from active thread usage / thread record when not explicitly provided (G3.2)
    if not author_model:
        author_model = await db_publications.resolve_thread_author_model(thread_id)

    async def _read_draft_and_bundle(
        dp: str, atts: list[dict[str, Any]] | None
    ) -> tuple[str, str | None, list[dict[str, Any]], list[tuple[str, str, bytes]], str] | None:
        """Read the draft (+ attachments) at ``dp``/``atts`` and hash the bundle;
        ``None`` when the draft file is missing. Round-4 decision 1: re-readable
        so an adopted request row's params can drive a fresh read."""
        text = await backend.aread_text(dp)
        if text is None:
            return None
        atts_meta: list[dict[str, Any]] = []
        blobs: list[tuple[str, str, bytes]] = []
        facts: str | None = None
        for att in atts or []:
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
                        facts = t
            elif raw is not None and arole == "facts":
                facts = raw.decode("utf-8", errors="replace")

            if raw is not None:
                asha = hashlib.sha256(raw).hexdigest()
                atts_meta.append({"path": apath, "role": arole, "sha256": asha})
                blobs.append((arole, apath, raw))
        return (
            text,
            facts,
            atts_meta,
            blobs,
            compute_bundle_sha256(text.encode("utf-8"), blobs),
        )

    read_result = await _read_draft_and_bundle(draft_path, attachments)
    if read_result is None:
        return {
            "ok": False,
            "message": f"ERROR: Draft file not found at {draft_path}",
        }
    draft_text, facts_text, att_list, att_blobs, bundle_sha256 = read_result
    # A5: the publish state machine re-audits through the same wall-clock budget
    # as check_draft — its provenance precheck, reuse hash and audit phases all
    # hang off this one deadline.
    audit_deadline = time.monotonic() + get_dalton_config().publish.audit_total_timeout_s

    latest_map = await db_knowledge.list_all_latest_for_workspace(
        workspace_id, valid_only=True
    )
    thesis_row = latest_map.get("thesis")
    thesis_doc = (thesis_row or {}).get("content")
    thesis_version_no = (thesis_row or {}).get("version_no")
    models_doc = (latest_map.get("models") or {}).get("content")
    company_doc = (latest_map.get("company") or {}).get("content") or {}

    # 行业主体（P3）：数据库层已可容纳无 ticker 主体（d027），但正式发布的
    # House View / 审批语义首期不开放——在这里、在用 slug 前缀伪造伪 ticker
    # 之前明确拒绝。开关关闭时不查注册表（解析用上面已加载的文档内容，是
    # 纯函数），公司路径与 P3 前逐字一致。
    from src.dalton.settings import is_coverage_units_enabled

    if is_coverage_units_enabled():
        from src.dalton.coverage.identity import resolve_workspace_coverage_identity

        pub_identity = await resolve_workspace_coverage_identity(
            ws_row,
            company_content=company_doc if isinstance(company_doc, dict) and company_doc else None,
            thesis_content=thesis_doc if isinstance(thesis_doc, dict) else None,
        )
        if pub_identity.is_sector:
            unit = pub_identity.unit or {}
            sector_label = (
                str(unit.get("display_name") or unit.get("slug") or "").strip()
                or workspace_id
            )
            return {
                "ok": False,
                "status": "sector_publish_not_supported",
                "subject_kind": "sector",
                "unit_id": str(unit.get("unit_id") or "") or None,
                "message": (
                    f"工作区 {workspace_id} 是行业覆盖单元（{sector_label}）。"
                    "行业主体的正式发布（House View 审批流）首期未开放：发布审批语义保持不变，"
                    "行业研究请以研究纪要 / 专题报告形式留在工作区（`check_draft` 数字核验仍可用）；"
                    "无 ticker 的行业主体行（d027）已可入库，待行业发布流程上线后启用。"
                ),
            }

    if request_id:
        # The request_id branch already adopted the row's params above.
        assert row is not None
        reuse_existing_row = True
    else:
        row, created = await db_publications.get_or_create_request(
            user_id=ws_owner_id,
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
        # Round-4 decision 1: a same-(thread, tool_call) retry that fetched an
        # EXISTING row adopts it too. The only exception is re-submitting one
        # of the retryable statuses without request_id — that path may carry
        # new params and writes them back (below).
        reuse_existing_row = not created and status not in (
            "audit_failed",
            "review_revise",
            "review_unavailable",
        )
        if reuse_existing_row:
            (
                title,
                kind,
                slug,
                draft_path,
                updates_thesis,
                attachments,
            ) = _adopt_request_params(
                row,
                title=title,
                kind=kind,
                slug=slug,
                draft_path=draft_path,
                updates_thesis=updates_thesis,
                attachments=attachments,
            )
            # The row's draft_path/attachments are authoritative — re-read the
            # content with them before any comparison or publication.
            re_read = await _read_draft_and_bundle(draft_path, attachments)
            if re_read is None:
                return {
                    "ok": False,
                    "status": status,
                    "audit": row.get("audit"),
                    "message": _prefix_with_audit_summary(
                        row,
                        f"按原请求参数读取草稿失败（draft_path={draft_path}），"
                        "正文已改动或文件缺失；请重新调用 publish_report"
                        "（不带 request_id）发起新的审批。",
                    ),
                }
            draft_text, facts_text, att_list, att_blobs, bundle_sha256 = re_read
        approved_thesis_version_no = row.get("thesis_version_no")

    ticker = str(company_doc.get("ticker") or slug.split("-")[0]).upper()

    if reuse_existing_row:
        # Round-3/4 decision 1: unified content gate — the bundle recomputed
        # from the row's draft_path/attachments as they sit on disk NOW must
        # still equal the row's recorded bundle_sha256, on EVERY entry that
        # fetched an existing row (request_id or same-tool-call retry alike).
        assert row is not None
        if str(row.get("bundle_sha256") or "") != bundle_sha256:
            err = "正文（草稿或附件）与请求记录的 bundle 哈希不一致，正文已改动"
            if status in ("approved", "publishing"):
                await db_publications.transition(
                    req_id, (status,), "superseded", error=err
                )
                return {
                    "ok": False,
                    "status": "superseded",
                    "audit": row.get("audit"),
                    "message": _prefix_with_audit_summary(
                        row,
                        f"{err}，已批准的结果作废（superseded），"
                        "请重新调用 publish_report 走审批。",
                    ),
                }
            if status == "published":
                return {
                    "ok": False,
                    "status": "published",
                    "audit": row.get("audit"),
                    "message": _prefix_with_audit_summary(
                        row,
                        "该请求已发布，但草稿/附件在此之后已改动（bundle 哈希不一致）；"
                        "如需发布新内容，请重新调用 publish_report（不带 request_id）"
                        "发起新的审批。",
                    ),
                }
            return {
                "ok": False,
                "status": status,
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"{err}；本请求未作任何改动，请重新调用 publish_report"
                    "（不带 request_id）发起新的审批。",
                ),
            }

    # Fast exits for terminal / already-completed states
    if status == "published":
        pub = (
            await db_publications.get_publication(str(row["publication_id"]))
            if row.get("publication_id")
            else None
        )
        paths = [f["path"] for f in (pub.get("files") or [])] if pub else []
        audit_summary = format_audit_summary_from_dict(
            row.get("audit") if isinstance(row.get("audit"), dict) else None
        )
        msg_prefix = f"{audit_summary} · " if audit_summary else ""
        # Round-3 decision 1: the saved publication's fields, not this call's.
        pub_title = str((pub or {}).get("title") or title)
        pub_ticker = str((pub or {}).get("ticker") or ticker)
        return {
            "ok": True,
            "status": "published",
            "publication_id": row.get("publication_id"),
            "paths": paths,
            "audit": row.get("audit"),
            "message": f"{msg_prefix}Published {pub_ticker} ({pub_title}): {', '.join(paths)} · publication {row.get('publication_id')}",
        }

    if status in ("rejected", "expired", "superseded"):
        return {
            "ok": False,
            "status": status,
            "audit": row.get("audit"),
            "message": _prefix_with_audit_summary(
                row,
                f"Publish request {req_id} is {status}: "
                f"{row.get('decision_message') or row.get('error') or 'please fix and call publish_report again.'}",
            ),
        }

    unrecalc_warnings = await _check_unrecalculated_models(
        backend,
        draft_text,
        facts_text,
        attachments=attachments,
        att_blobs=att_blobs,
    )
    prov_records: dict[str, dict[str, Any]] | None = None
    model_cells: dict[str, dict[str, Any]] | None = None
    evidence_ref_bodies: dict[str, str] | None = None
    if status in ("audit_failed", "review_revise", "review_unavailable") and not created:
        if request_id is not None:
            # Round-5 item 2: an EXPLICIT request_id re-entry never rewrites
            # the row's params or pinned thesis version — that licence is the
            # same-tool-call retry's alone. A drifted thesis is refused here:
            # re-file instead of re-auditing under a stale pin.
            drift_err = _thesis_version_drift(row, thesis_row)
            if drift_err:
                return {
                    "ok": False,
                    "status": status,
                    "audit": row.get("audit"),
                    "message": _prefix_with_audit_summary(
                        row,
                        f"{drift_err}；请重新调用 publish_report（不带 request_id）"
                        "发起新的审批。",
                    ),
                }
        prov_records = await _load_workspace_provenance(
            workspace_id,
            draft_path=draft_path,
            draft_text=draft_text,
            referenced_ids=_extract_referenced_prov_ids(draft_text, facts_text),
            deadline=audit_deadline,
        )
        model_cells = await _load_referenced_model_cells(
            backend, draft_text, facts_text, models_doc
        )
        evidence_ref_bodies = await _load_evidence_ref_bodies(
            backend, thesis_doc, draft_path=draft_path
        )
        try:
            current_inputs_sha = await run_in_heavy_subprocess(
                compute_audit_inputs_sha256,
                draft_markdown=draft_text,
                facts_markdown=facts_text,
                thesis_doc=thesis_doc,
                models_doc=models_doc,
                model_cells=model_cells,
                provenance_records=prov_records,
                evidence_ref_bodies=evidence_ref_bodies,
                deadline=audit_deadline,
                label="publish.inputs_sha",
            )
        except HeavyComputeTimeout:
            # Budget gone before the reuse hash: skip the reuse comparison; the
            # re-audit below runs under the same deadline.
            current_inputs_sha = ""
        _, _, _, current_cfg_sha = await resolve_audit_config_fingerprint(user_id, use_llm_judge=True)
        prev_audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        prev_inputs_sha = str(prev_audit.get("inputs_sha256") or "")
        prev_cfg_sha = str(prev_audit.get("config_sha256") or "")
        prev_unrecalc = list(prev_audit.get("unrecalculated_model_warnings") or [])
        if (
            row.get("bundle_sha256") == bundle_sha256
            and bool(prev_inputs_sha)
            and prev_inputs_sha == current_inputs_sha
            and bool(prev_cfg_sha)
            and prev_cfg_sha == current_cfg_sha
            and prev_unrecalc == unrecalc_warnings
        ):
            audit_path, annotated_path = _derive_sidecar_paths(draft_path)
            audit_summary = format_audit_summary_from_dict(
                prev_audit,
                audit_path=audit_path if status == "audit_failed" else None,
            )
            unrecalc_lead = f"{'；'.join(unrecalc_warnings)}。" if unrecalc_warnings else ""
            msg_prefix = f"{unrecalc_lead}{audit_summary}。" if audit_summary else unrecalc_lead
            # 提示词第 1 批第 2 轮 #5：同请求幂等重入时，早退分支也要沿用
            # block 的阻断文案——"remains in review_revise" 说不出需要人工处理。
            review_info = row.get("review") if isinstance(row.get("review"), dict) else {}
            if status == "review_revise" and review_info.get("verdict") == "block":
                block_fixes = "; ".join(
                    review_info.get("must_fix") or [review_info.get("summary") or ""]
                )
                message = (
                    f"{msg_prefix}发布请求 {req_id} 的独立审稿结论仍为 block：存在阻断性问题，"
                    f"不能靠常规修订解决（{block_fixes}）。请停止发布，向 PM 说明这些问题；"
                    "等人工处理完，再重新调用 publish_report（不带 request_id）发起新的审批。"
                )
            else:
                message = (
                    f"{msg_prefix}Publish request {req_id} remains in {status}; "
                    "update the draft before retrying."
                )
            return {
                "ok": False,
                "status": status,
                "audit": prev_audit or None,
                "audit_path": audit_path,
                "annotated_path": annotated_path,
                "message": message,
            }
        # Bundle, inputs, or audit config changed -> re-enter auditing.
        # Round-3 decision 1: a same-tool-call retry (no request_id) may carry
        # new params — write them all back so the row always reflects the
        # latest submission that went through the audit. Round-5 item 2: an
        # explicit request_id re-entry keeps the row's saved params/version
        # (they were adopted above) and only moves the status.
        if request_id is None:
            updated = await db_publications.transition(
                req_id,
                (status,),
                "auditing",
                bundle_sha256=bundle_sha256,
                attachments=att_list,
                title=title,
                kind=kind,
                slug=slug,
                draft_path=draft_path,
                updates_thesis=updates_thesis,
                thesis_version_no=thesis_version_no if updates_thesis else None,
            )
        else:
            updated = await db_publications.transition(
                req_id, (status,), "auditing"
            )
        if updated:
            row = updated
            status = "auditing"
            # Round-4 decision 2: the re-submission may have re-pinned the
            # thesis version — every later phase reads it from the updated row.
            approved_thesis_version_no = row.get("thesis_version_no")

    # Step 2: Figure Audit (when status == 'auditing')
    if status == "auditing":
        if prov_records is None:
            prov_records = await _load_workspace_provenance(
                workspace_id,
                draft_path=draft_path,
                draft_text=draft_text,
                referenced_ids=_extract_referenced_prov_ids(draft_text, facts_text),
                deadline=audit_deadline,
            )
        if model_cells is None:
            model_cells = await _load_referenced_model_cells(
                backend, draft_text, facts_text, models_doc
            )
        if evidence_ref_bodies is None:
            evidence_ref_bodies = await _load_evidence_ref_bodies(
                backend, thesis_doc, draft_path=draft_path
            )
        audit_path, annotated_path = _derive_sidecar_paths(draft_path)
        existing_audit_md = await _read_optional_text(backend, audit_path)
        try:
            audit = await run_in_heavy_subprocess(
                try_load_reusable_audit_from_md,
                existing_audit_md,
                draft_markdown=draft_text,
                thesis_doc=thesis_doc,
                models_doc=models_doc,
                model_cells=model_cells,
                provenance_records=prov_records,
                facts_markdown=facts_text,
                evidence_ref_bodies=evidence_ref_bodies,
                use_llm_judge=True,
                config_fingerprint=await resolve_audit_config_fingerprint(
                    user_id, use_llm_judge=True
                ),
                deadline=audit_deadline,
                label="publish.audit_cache_check",
            )
        except HeavyComputeTimeout:
            audit = None
        if audit is None:
            try:
                audit = await aaudit_draft(
                    draft_text,
                    thesis_doc=thesis_doc,
                    models_doc=models_doc,
                    model_cells=model_cells,
                    provenance_records=prov_records,
                    facts_markdown=facts_text,
                    evidence_ref_bodies=evidence_ref_bodies,
                    draft_path=draft_path,
                    use_llm_judge=True,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    deadline=audit_deadline,
                )
            except AuditBuildTimeout as e:
                # A5 review: the request is already 'auditing'; without this
                # settle the row would sit there forever. Same transition the
                # not-passed audit takes, with the failure wording saved.
                logger.error(
                    "Audit build timed out for request %s: %s", req_id, e
                )
                await db_publications.transition(
                    req_id, ("auditing",), "audit_failed", error=str(e)
                )
                return {
                    "ok": False,
                    "status": "audit_failed",
                    "reused": False,
                    "message": str(e),
                }
            audit.unrecalculated_model_warnings = unrecalc_warnings
            await backend.awrite_text(
                audit_path, await asyncio.to_thread(render_audit_md, audit, draft_path=draft_path)
            )
            await backend.awrite_text(
                annotated_path, await asyncio.to_thread(render_annotated_draft, draft_text, audit)
            )
        else:
            audit.unrecalculated_model_warnings = unrecalc_warnings
            if unrecalc_warnings and any(
                w not in (existing_audit_md or "") for w in unrecalc_warnings
            ):
                await backend.awrite_text(
                    audit_path,
                    await asyncio.to_thread(render_audit_md, audit, draft_path=draft_path),
                )

        if not audit.passed or unrecalc_warnings:
            unrecalc_lead = f"{'；'.join(unrecalc_warnings)}。" if unrecalc_warnings else ""
            await db_publications.transition(
                req_id,
                ("auditing",),
                "audit_failed",
                audit=audit.to_dict(),
                error="；".join(unrecalc_warnings) if unrecalc_warnings else None,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "reused": audit.reused,
                "audit": audit.to_dict(),
                "audit_path": audit_path,
                "annotated_path": annotated_path,
                "message": (
                    f"{unrecalc_lead}{audit.summary_line(audit_path)}。"
                    f"请根据 {audit_path} 修复冲突与未核实数字后重新调用 publish_report。"
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
        # Round-4 decision 2: thesis drift is caught BEFORE the review runs —
        # a drifted thesis must be re-filed, not reviewed and voided later.
        drift_err = _thesis_version_drift(row, thesis_row)
        if drift_err:
            return {
                "ok": False,
                "status": status,
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"{drift_err}；请重新调用 publish_report（不带 request_id）"
                    "发起新的审批。",
                ),
            }
        if unrecalc_warnings:
            err_msg = "；".join(unrecalc_warnings)
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "audit_failed",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, err_msg),
            }
        model_sha_err = await _verify_referenced_model_sha256s(
            backend, draft_text, facts_text, row.get("audit")
        )
        if model_sha_err:
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "superseded",
                error=model_sha_err,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, model_sha_err),
            }
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
                user_id=ws_owner_id,
                workspace_id=workspace_id,
                what="配置审稿模型凭证",
                kind="data_access",
                backend=backend,
            )
            return {
                "ok": False,
                "status": "review_unavailable",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"无法发布：缺少独立审稿（{rev_res.reason}）。"
                    f"草稿保留在 {draft_path}，审稿恢复后再调用。",
                ),
            }

        if rev_dict.get("verdict") in ("revise", "block"):
            # block reuses the review_revise status (no new state-machine state,
            # no migration): like revise it never reaches approval, but the
            # verdict stays "block" in the review payload and the message says
            # the problem is unfixable by routine revision and needs a human.
            is_block = rev_dict.get("verdict") == "block"
            await db_publications.transition(
                req_id,
                ("reviewing",),
                "review_revise",
                review=rev_dict,
            )
            fixes = "; ".join(rev_dict.get("must_fix") or [rev_dict.get("summary", "")])
            if is_block:
                message = (
                    f"独立审稿（{rev_dict.get('model')} / {rev_dict.get('family')}）"
                    f"结论为 block：存在阻断性问题，不能靠常规修订解决（{fixes}）。"
                    "请停止发布，向 PM 说明这些问题（事实错误、数字对不上或结论没有证据支撑）；"
                    "等人工处理完，再重新调用 publish_report（不带 request_id）发起新的审批。"
                )
            else:
                message = (
                    f"Independent review ({rev_dict.get('model')} / {rev_dict.get('family')}) "
                    f"requested revisions: {fixes}"
                )
            return {
                "ok": False,
                "status": "review_revise",
                "audit": row.get("audit"),
                "review": rev_dict,
                "message": _prefix_with_audit_summary(row, message),
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
        # Round-4 decision 2: thesis drift is caught BEFORE the approval card
        # (or the unattended handoff) — a drifted thesis must be re-filed, not
        # approved and voided afterwards.
        drift_err = _thesis_version_drift(row, thesis_row)
        if drift_err:
            return {
                "ok": False,
                "status": status,
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"{drift_err}；请重新调用 publish_report（不带 request_id）"
                    "发起新的审批。",
                ),
            }
        if unrecalc_warnings:
            err_msg = "；".join(unrecalc_warnings)
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "audit_failed",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, err_msg),
            }
        model_sha_err = await _verify_referenced_model_sha256s(
            backend, draft_text, facts_text, row.get("audit")
        )
        if model_sha_err:
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "superseded",
                error=model_sha_err,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, model_sha_err),
            }

        # 4a. Unattended run -> do NOT emit interrupt(); record pm_handoff and stop at awaiting_pm.
        # Round-2 decision A: a colleague-initiated publish takes this path too —
        # the workspace owner cannot answer an approval card inside a colleague's
        # thread (read-only there), and the colleague cannot pass the PM check.
        initiated_by = await _resolve_publish_initiator(
            row,
            ws_owner_id,
            caller_thread_id=thread_id,
            caller_user_id=user_id,
        )
        if origin_type in ("automation", "agent") or user_id != ws_owner_id:
            await _append_agenda_pm_handoff(
                user_id=ws_owner_id,
                workspace_id=workspace_id,
                what=(
                    f"批准正式发布《{title}》（request_id={req_id}"
                    + (f"，由 {initiated_by} 发起" if initiated_by else "")
                    + "）"
                ),
                kind="publish_approval",
                backend=backend,
            )
            audit_summary = format_audit_summary_from_dict(
                row.get("audit") if isinstance(row.get("audit"), dict) else None
            )
            msg_prefix = f"{audit_summary}。" if audit_summary else ""
            if user_id != ws_owner_id:
                message = (
                    f"{msg_prefix}已通过数字核验与独立审稿，已提交给工作区所有者审批"
                    f"（request_id={req_id}）；所有者在自己的线程里用这个 request_id "
                    "继续即可审批。"
                )
            else:
                message = (
                    f"{msg_prefix}已通过数字核验与独立审稿，等待 PM 审批，"
                    f"request_id={req_id}"
                )
            return {
                "ok": False,
                "status": "awaiting_pm",
                "request_id": req_id,
                "audit": row.get("audit"),
                "message": message,
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
        incidental_cnt = int(
            audit_info.get("incidental", len(audit_info.get("incidental_items") or []))
        )
        contradicted_items = list(audit_info.get("contradicted_items") or [])
        uncertain_items = list(audit_info.get("uncertain_items") or [])
        unverified_items = list(audit_info.get("unverified_items") or [])
        llm_verified_items = list(audit_info.get("llm_verified_items") or [])
        incidental_items = list(audit_info.get("incidental_items") or [])
        not_required_cnt = int(
            audit_info.get("not_required", exempt_cnt + incidental_cnt)
        )
        reused_flag = bool(audit_info.get("reused", False))
        labeling = dict(audit_info.get("labeling") or {})
        verified_by_counts = dict(
            audit_info.get("verified_by_counts")
            or {"footnote": 0, "llm_judge": 0, "derived": 0}
        )

        labeling_incomplete = bool(audit_info.get("labeling_incomplete", False))

        figure_summary = {
            "verified": verified_cnt,
            "contradicted": contradicted_cnt,
            "unverified": unverified_cnt,
            "uncertain": uncertain_cnt,
            "exempt": exempt_cnt,
            "incidental": incidental_cnt,
            "not_required": not_required_cnt,
            "reused": reused_flag,
            "labeling_incomplete": labeling_incomplete,
            "labeling": labeling,
            "verified_by_counts": verified_by_counts,
            "contradicted_items": contradicted_items,
            "uncertain_items": uncertain_items,
            "unverified_items": unverified_items,
            "llm_verified_items": llm_verified_items,
            "incidental_items": incidental_items,
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
        audit_summary_str = format_audit_summary_from_dict(audit_info)
        reviewer_summary_str = (
            f"通过 · {reviewer_payload['model']} （{reviewer_payload['family']}）"
        )
        thesis_ver_str = (
            f"v{approved_thesis_version_no}"
            if updates_thesis and approved_thesis_version_no
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
                        "unverified_items": unverified_items,
                        "llm_verified_items": llm_verified_items,
                        "incidental_items": incidental_items,
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
        if initiated_by:
            # A colleague-initiated publish: the request belongs to the
            # workspace owner, so the approval card names who actually filed it.
            payload["action_requests"][0]["args"]["initiated_by"] = initiated_by
            payload["action_requests"][0]["args"]["发起人"] = initiated_by
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

        # Verify PM user authorization. Round-2 decision B: approve/reject is
        # the workspace owner's alone — another PM-listed colleague may not
        # decide someone else's workspace.
        pm_users = set(get_dalton_config().pm_user_ids)
        if user_id not in pm_users or user_id != ws_owner_id:
            return {
                "ok": False,
                "status": "awaiting_pm",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    "只有工作区所有者（且在 PM 名单内）能批准或拒绝正式发布，"
                    "当前调用者不是该工作区的所有者。",
                ),
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
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"PM 拒绝发布：{dec_msg or '无附加说明'}",
                ),
            }

        post_pm_unrecalc = await _check_unrecalculated_models(
            backend, draft_text, facts_text, attachments=attachments, att_blobs=None
        )
        if post_pm_unrecalc:
            err_msg = "；".join(post_pm_unrecalc)
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "audit_failed",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, err_msg),
            }
        post_pm_sha_err = await _verify_referenced_model_sha256s(
            backend, draft_text, facts_text, row.get("audit")
        )
        if post_pm_sha_err:
            await db_publications.transition(
                req_id,
                ("awaiting_pm",),
                "superseded",
                error=post_pm_sha_err,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, post_pm_sha_err),
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
        # Round-3 decision 1: if the approval pinned a thesis version, the
        # workspace's current thesis must still be that version — a drift means
        # the approved thesis.official snapshot is stale; go back to approval.
        thesis_drift = _thesis_version_drift(row, thesis_row)
        if thesis_drift:
            await db_publications.transition(
                req_id,
                ("approved",),
                "superseded",
                error=thesis_drift,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"{thesis_drift}，已批准的结果作废（superseded），"
                    "请重新调用 publish_report 走审批。",
                ),
            }
        app_unrecalc = await _check_unrecalculated_models(
            backend, draft_text, facts_text, attachments=attachments, att_blobs=None
        )
        if app_unrecalc:
            err_msg = "；".join(app_unrecalc)
            await db_publications.transition(
                req_id,
                ("approved",),
                "audit_failed",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, err_msg),
            }
        app_sha_err = await _verify_referenced_model_sha256s(
            backend, draft_text, facts_text, row.get("audit")
        )
        if app_sha_err:
            await db_publications.transition(
                req_id,
                ("approved",),
                "superseded",
                error=app_sha_err,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, app_sha_err),
            }
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
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    "草稿在审批后被改动，请重新发布（superseded）。",
                ),
            }
        row = claimed
        status = "publishing"

    # Step 7, 8, 9: Write immutable files, insert publication rows, update thesis.official
    if status == "publishing":
        # Round-3 decision 1: the publishing resume path has no
        # consume_approval of its own, so it re-checks the thesis drift here
        # (the bundle gate already ran before the state dispatch).
        thesis_drift = _thesis_version_drift(row, thesis_row)
        if thesis_drift:
            await db_publications.transition(
                req_id,
                ("publishing",),
                "superseded",
                error=thesis_drift,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(
                    row,
                    f"{thesis_drift}，已批准的结果作废（superseded），"
                    "请重新调用 publish_report 走审批。",
                ),
            }
        pub_unrecalc = await _check_unrecalculated_models(
            backend, draft_text, facts_text, attachments=attachments, att_blobs=None
        )
        if pub_unrecalc:
            err_msg = "；".join(pub_unrecalc)
            await db_publications.transition(
                req_id,
                ("publishing",),
                "audit_failed",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "audit_failed",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, err_msg),
            }
        pub_sha_err = await _verify_referenced_model_sha256s(
            backend, draft_text, facts_text, row.get("audit")
        )
        if pub_sha_err:
            await db_publications.transition(
                req_id,
                ("publishing",),
                "superseded",
                error=pub_sha_err,
            )
            return {
                "ok": False,
                "status": "superseded",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, pub_sha_err),
            }
        now_dt = datetime.now(timezone.utc)
        pub_date_str = now_dt.strftime("%Y-%m-%d")
        approved_at_iso = (
            now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        review_info = row.get("review") or {}
        audit_info = row.get("audit") or {}
        approver = str(row.get("decided_by") or ws_owner_id)

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
            thesis_version_no=approved_thesis_version_no if updates_thesis else None,
        )

        files_meta, file_blobs, file_notes = await write_published_bundle(
            slug=slug,
            published_date=pub_date_str,
            published_markdown=pub_markdown,
            attachments=att_list,
            backend=backend,
        )

        try:
            pub_row = await db_publications.insert_publication(
                publish_request_id=req_id,
                user_id=ws_owner_id,
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
                thesis_version_no=approved_thesis_version_no
                if updates_thesis
                else None,
            )
        except db_publications.WorkspaceUnavailableForPublishError as exc:
            err_msg = str(exc)
            await db_publications.transition(
                req_id,
                ("publishing",),
                "rejected",
                error=err_msg,
            )
            return {
                "ok": False,
                "status": "rejected",
                "audit": row.get("audit"),
                "message": _prefix_with_audit_summary(row, f"ERROR: {err_msg}"),
            }
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
                user_id=ws_owner_id,
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
        audit_summary = format_audit_summary_from_dict(
            row.get("audit") if isinstance(row.get("audit"), dict) else None
        )
        msg_prefix = f"{audit_summary} · " if audit_summary else ""
        res_out: dict[str, Any] = {
            "ok": True,
            "status": "published",
            "publication_id": pub_id,
            "paths": [f["path"] for f in files_meta],
            "audit": row.get("audit"),
            "message": (
                f"{msg_prefix}Published {ticker} {title}: {paths_str}"
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
    interrupt_fn: Any = None,
) -> dict[str, Any]:
    """Execute the PK.11 publication state machine (see ``_run_publish_state_machine``).

    Round-3 decision 1 wrapper: when a request_id re-entry ignored some of the
    caller's params in favour of the row's saved ones, every result message is
    prefixed with which params were ignored, so an owner taking a request over
    sees that their (differing) title/slug/etc. did not take effect."""
    # One shape for "no request_id" everywhere below: the tool may pass "",
    # and the state machine mixes truthiness and `is None` checks.
    request_id = (request_id or "").strip() or None
    token = _IGNORED_PARAMS_NOTE.set(None)
    try:
        result = await _run_publish_state_machine(
            user_id=user_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            draft_path=draft_path,
            title=title,
            kind=kind,
            slug=slug,
            attachments=attachments,
            updates_thesis=updates_thesis,
            request_id=request_id,
            backend=backend,
            author_model=author_model,
            origin_type=origin_type,
            interrupt_fn=interrupt_fn,
        )
    finally:
        ignored = _IGNORED_PARAMS_NOTE.get()
        _IGNORED_PARAMS_NOTE.reset(token)
    if ignored and isinstance(result, dict):
        result["message"] = (
            f"已按原请求的参数继续，忽略本次传入的 {'、'.join(ignored)}。"
            f"{result.get('message') or ''}"
        )
        result["ignored_params"] = list(ignored)
    return result
