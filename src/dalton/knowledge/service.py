"""Company knowledge layer service: apply updates, append verdicts, render files, and import (PK.4, PK.6, PK.7)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.database.knowledge import VersionConflict
from src.dalton.knowledge.ops import (
    OpError,
    _ensure_nested_ids_and_defaults,
    apply_ops,
    diff_documents,
    repair_knowledge_ops,
    suggest_close_fields,
)
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
from src.dalton.knowledge.validate import (
    _load_schemas_and_registry,
    validate_update,
    validate_verdict,
)
from src.dalton.llm.assist import assist_json

logger = logging.getLogger(__name__)

MAX_AGENT_MD_WARN_CHARS = int(32768 * 0.85)

_REPAIR_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        }
    },
    "required": ["mappings"],
}


def canonical_json(doc: dict[str, Any]) -> tuple[str, str]:
    """Serialize ``doc`` canonically and return ``(text, sha256)``.

    Keys are sorted because the content makes a jsonb round-trip between hashing and
    writing ``data/<doc>.json``, and jsonb reorders keys; an order-dependent text gave a
    stored ``content_sha256`` that never matched the written file, which reconcile then
    read as a direct edit.
    """
    text = (
        json.dumps(
            doc, sort_keys=True, ensure_ascii=False, indent=2, separators=(",", ": ")
        )
        + "\n"
    )
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


def _normalize_legacy_aliases(
    doc_key: str,
    doc: dict[str, Any],
    *,
    old_doc: dict[str, Any] | None = None,
) -> None:
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
        old_ev_list = (old_doc or {}).get("evidence") or (old_doc or {}).get("evidence_log") or []
        old_ev_ids = {
            str(item.get("id"))
            for item in (old_ev_list if isinstance(old_ev_list, list) else [])
            if isinstance(item, dict) and item.get("id")
        }
        for ev in doc.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            ev_id = str(ev.get("id") or "")
            if ev_id and ev_id in old_ev_ids:
                continue
            src = ev.get("source")
            if isinstance(src, dict) and src.get("type") == "model":
                ref = src.get("ref")
                if isinstance(ref, dict):
                    if "file" in ref and "path" not in ref:
                        ref["path"] = ref.pop("file")
                    if "sheet" in ref:
                        sheet = ref.pop("sheet")
                        cell = ref.get("cell") or ""
                        if sheet and cell and "!" not in str(cell):
                            ref["cell"] = f"{sheet}!{cell}"


def _resolve_container_at_path(doc: dict[str, Any], path: str) -> Any:
    curr: Any = doc
    for seg in [s for s in path.split("/") if s]:
        if isinstance(curr, dict) and seg in curr:
            curr = curr[seg]
        elif isinstance(curr, list) and seg.isdigit() and 0 <= int(seg) < len(curr):
            curr = curr[int(seg)]
        else:
            return None
    return curr


async def _repair_ops_paths_with_assist(
    doc_key: str,
    base_doc: dict[str, Any] | None,
    ops: list[dict[str, Any]],
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use difflib suggestions + assist_json('knowledge.repair_field') to confirm unknown top-level op paths."""
    if not isinstance(base_doc, dict) or not ops:
        return ops, []

    valid_keys = [k for k in base_doc.keys() if k != "official"]
    unknown_tops: list[str] = []
    for raw_op in ops:
        if not isinstance(raw_op, dict) or raw_op.get("op") == "create":
            continue
        path = str(raw_op.get("path") or "")
        segs = [s for s in path.split("/") if s]
        if segs and segs[0] not in base_doc:
            top = segs[0]
            if top not in unknown_tops:
                unknown_tops.append(top)

    if not unknown_tops:
        return ops, []

    unknown_suggestions = suggest_close_fields(unknown_tops, valid_keys)

    res = await assist_json(
        task="knowledge.repair_field",
        instructions=(
            "将知识库更新操作中未识别的顶层路径字段名映射为文档中的合法字段名。"
            "参考 difflib_suggestions 中的候选建议，确认后返回 {\"mappings\": {\"原字段名\": \"合法字段名\"}}。"
        ),
        payload={
            "doc_key": doc_key,
            "unknown_fields": unknown_tops,
            "difflib_suggestions": unknown_suggestions,
            "schema_properties": valid_keys,
        },
        schema=_REPAIR_FIELD_SCHEMA,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    if not (res.ok and isinstance(res.data, dict) and isinstance(res.data.get("mappings"), dict)):
        return ops, []

    mappings = {
        str(k).lstrip("/"): str(v).lstrip("/")
        for k, v in res.data["mappings"].items()
        if k and v and str(v).lstrip("/") in valid_keys
    }
    if not mappings:
        return ops, []

    repaired: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for raw_op in ops:
        if not isinstance(raw_op, dict):
            repaired.append(raw_op)
            continue
        op_copy = copy.deepcopy(raw_op)
        path = str(op_copy.get("path") or "")
        segs = [s for s in path.split("/") if s]
        if segs and segs[0] in mappings:
            old_top = segs[0]
            new_top = mappings[old_top]
            suffix = ("/" + "/".join(segs[1:])) if len(segs) > 1 else ""
            new_path = f"/{new_top}{suffix}"
            op_copy["path"] = new_path
            warnings.append(
                {
                    "code": "path_repaired",
                    "path": path,
                    "original_field": f"/{old_top}",
                    "repaired_to": f"/{new_top}",
                    "message": f"repaired path {path!r} -> {new_path!r} via knowledge.repair_field",
                }
            )
        repaired.append(op_copy)
    return repaired, warnings


def _deref_schema_node(
    node: Any,
    cur_root: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve local ``#/$defs/...`` and cross-schema ``<doc>#/$defs/...`` references."""
    if not isinstance(node, dict):
        return {}, cur_root
    curr = node
    root = cur_root
    for _ in range(8):
        ref = curr.get("$ref")
        if not isinstance(ref, str):
            break
        if ref.startswith("#/$defs/"):
            def_name = ref.split("#/$defs/", 1)[1]
            target = (root.get("$defs") or {}).get(def_name)
            if isinstance(target, dict):
                curr = target
                continue
            break
        if "#/$defs/" in ref:
            ext_key, def_name = ref.split("#/$defs/", 1)
            ext_root = schemas.get(ext_key) or {}
            target = (ext_root.get("$defs") or {}).get(def_name)
            if isinstance(target, dict):
                curr = target
                root = ext_root
                continue
            break
        break
    return curr, root


def _describe_schema_field(
    prop_node: Any,
    cur_root: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
) -> str:
    """Format a compact single-line description of a schema property's type, enum, and nested constraints."""
    resolved, eff_root = _deref_schema_node(prop_node, cur_root, schemas)
    raw_type = resolved.get("type")
    union_branches = resolved.get("oneOf") or resolved.get("anyOf")

    obj_branch: dict[str, Any] | None = (
        resolved if resolved.get("type") == "object" and isinstance(resolved.get("properties"), dict) else None
    )
    enum_vals: list[Any] | None = (
        resolved["enum"] if isinstance(resolved.get("enum"), list) else None
    )

    if isinstance(union_branches, list) and union_branches:
        branch_types: list[str] = []
        has_iso_date = False
        has_null = False
        for b in union_branches:
            if not isinstance(b, dict):
                continue
            b_ref = str(b.get("$ref") or "")
            if b_ref.endswith("/isoDate"):
                has_iso_date = True
            b_res, _ = _deref_schema_node(b, eff_root, schemas)
            bt = b_res.get("type")
            if isinstance(bt, list):
                for x in bt:
                    s = str(x)
                    if s == "null":
                        has_null = True
                    elif s not in branch_types:
                        branch_types.append(s)
            elif isinstance(bt, str):
                if bt == "null":
                    has_null = True
                elif bt not in branch_types:
                    branch_types.append(bt)
            elif isinstance(b_res.get("enum"), list):
                if "string" not in branch_types:
                    branch_types.append("string")
            if b_res.get("type") == "object" and isinstance(b_res.get("properties"), dict) and obj_branch is None:
                obj_branch = b_res
            if enum_vals is None and isinstance(b_res.get("enum"), list):
                enum_vals = b_res["enum"]
        if has_iso_date:
            base_union = ["date" if t == "string" else t for t in branch_types] or ["date"]
            if has_null:
                base_union.append("null")
            type_str = "|".join(base_union) + " (string)"
        else:
            if has_null:
                branch_types.append("null")
            type_str = "|".join(branch_types) if branch_types else "any"
    elif isinstance(raw_type, list):
        type_str = "|".join(str(t) for t in raw_type)
    elif raw_type == "array" and isinstance(resolved.get("items"), dict):
        item_res, _ = _deref_schema_node(resolved["items"], eff_root, schemas)
        item_t = item_res.get("type")
        if not item_t and isinstance(item_res.get("enum"), list):
            item_t = "string"
        item_t = item_t or "any"
        if isinstance(item_t, list):
            item_t = "|".join(str(x) for x in item_t)
        type_str = f"array<{item_t}>"
        if isinstance(item_res.get("enum"), list):
            type_str += f" (items enum={json.dumps(item_res['enum'], ensure_ascii=False)})"
    elif isinstance(raw_type, str):
        ref_str = str(prop_node.get("$ref") or "") if isinstance(prop_node, dict) else ""
        if ref_str.endswith("/isoDate"):
            type_str = "date (string)"
        else:
            type_str = raw_type
    elif "const" in resolved:
        type_str = f"const={json.dumps(resolved['const'], ensure_ascii=False)}"
    elif enum_vals is not None:
        type_str = "string"
    else:
        type_str = "any"

    parts = [type_str]
    if enum_vals is not None:
        parts.append(f"enum={json.dumps(enum_vals, ensure_ascii=False)}")
    if obj_branch is not None:
        sub_req = obj_branch.get("required") or []
        if sub_req:
            parts.append(f"required={json.dumps(sub_req, ensure_ascii=False)}")
        if "additionalProperties" in obj_branch:
            parts.append(f"additionalProperties={json.dumps(obj_branch['additionalProperties'])}")
    return ", ".join(parts)


def _format_expected_structures(
    doc_key: str,
    errors: list[dict[str, Any]],
    *,
    max_chars: int = 1500,
) -> str:
    """Extract expected JSON Schema object structure (required, field types, enum, additionalProperties) for failing paths."""
    if not errors:
        return ""
    schemas, _ = _load_schemas_and_registry()
    doc_schema = schemas.get(doc_key)
    if not isinstance(doc_schema, dict):
        return ""

    root_node, root_ctx = _deref_schema_node(doc_schema, doc_schema, schemas)
    seen_paths: set[str] = set()
    blocks: list[str] = []

    for err in errors:
        if not isinstance(err, dict):
            continue
        err_path = str(err.get("path") or "/")
        segs = [s for s in err_path.split("/") if s]
        cur_node, cur_root = root_node, root_ctx
        history: list[tuple[str, dict[str, Any], dict[str, Any]]] = [("/", cur_node, cur_root)]
        built_segs: list[str] = []

        for seg in segs:
            cur_node, cur_root = _deref_schema_node(cur_node, cur_root, schemas)
            if seg.isdigit() and isinstance(cur_node.get("items"), dict):
                cur_node, cur_root = _deref_schema_node(cur_node["items"], cur_root, schemas)
                built_segs.append(seg)
                history.append(("/" + "/".join(built_segs), cur_node, cur_root))
            elif isinstance(cur_node.get("properties"), dict) and seg in cur_node["properties"]:
                cur_node, cur_root = _deref_schema_node(
                    cur_node["properties"][seg], cur_root, schemas
                )
                built_segs.append(seg)
                history.append(("/" + "/".join(built_segs), cur_node, cur_root))
            elif isinstance(cur_node.get("items"), dict):
                item_node, item_root = _deref_schema_node(cur_node["items"], cur_root, schemas)
                if isinstance(item_node.get("properties"), dict) and seg in item_node["properties"]:
                    cur_node, cur_root = _deref_schema_node(
                        item_node["properties"][seg], item_root, schemas
                    )
                    built_segs.append(seg)
                    history.append(("/" + "/".join(built_segs), cur_node, cur_root))
                else:
                    break
            else:
                break

        target_path, target_node, target_root = history[-1]
        if not isinstance(target_node.get("properties"), dict):
            if isinstance(target_node.get("items"), dict):
                item_node, item_root = _deref_schema_node(
                    target_node["items"], target_root, schemas
                )
                if isinstance(item_node.get("properties"), dict):
                    target_path = f"{target_path.rstrip('/')}/*"
                    target_node, target_root = item_node, item_root
            if not isinstance(target_node.get("properties"), dict):
                for h_path, h_node, h_root in reversed(history[:-1]):
                    if isinstance(h_node.get("properties"), dict):
                        target_path, target_node, target_root = h_path, h_node, h_root
                        break

        if not isinstance(target_node.get("properties"), dict) or target_path in seen_paths:
            continue
        seen_paths.add(target_path)

        req_fields = [k for k in (target_node.get("required") or []) if k != "official"]
        add_props = target_node.get("additionalProperties", True)
        props = {
            k: v
            for k, v in (target_node.get("properties") or {}).items()
            if k != "official"
        }
        req_set = set(req_fields)
        lines = [
            f"Expected structure for {target_path} "
            f"(required={json.dumps(req_fields, ensure_ascii=False)}, "
            f"additionalProperties={json.dumps(add_props)}):"
        ]
        for fname, fschema in props.items():
            fdesc = _describe_schema_field(fschema, target_root, schemas)
            req_tag = " [required]" if fname in req_set else ""
            lines.append(f"  {fname}: {fdesc}{req_tag}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    combined = "\n".join(blocks)
    if len(combined) > max_chars:
        ellipsis_line = "…（其余字段省略）"
        budget = max_chars - len(ellipsis_line) - 1
        kept_lines: list[str] = []
        cur_len = 0
        for line in combined.splitlines():
            add_len = len(line) if not kept_lines else len(line) + 1
            if cur_len + add_len > budget:
                break
            kept_lines.append(line)
            cur_len += add_len
        if not kept_lines:
            return ellipsis_line[:max_chars]
        return "\n".join(kept_lines) + "\n" + ellipsis_line
    return combined


def _schema_props_for_path(schema_obj: dict[str, Any], err_path: str) -> list[str]:
    """Return allowed property names for the schema container at ``err_path`` (resolving local ``#/$defs/``)."""
    defs = schema_obj.get("$defs") or {}

    def _deref(node: Any) -> dict[str, Any]:
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            def_name = ref.split("#/$defs/", 1)[1]
            target = defs.get(def_name)
            if isinstance(target, dict):
                return target
        return node

    curr: dict[str, Any] = _deref(schema_obj)
    for seg in [s for s in str(err_path or "/").split("/") if s]:
        curr = _deref(curr)
        if seg.isdigit():
            items_node = curr.get("items")
            curr = _deref(items_node)
        else:
            props = curr.get("properties") or {}
            if seg in props:
                curr = _deref(props[seg])
            elif isinstance(curr.get("items"), dict):
                item_schema = _deref(curr["items"])
                item_props = item_schema.get("properties") or {}
                curr = _deref(item_props.get(seg, {}))
            else:
                return []
    curr = _deref(curr)
    return [k for k in (curr.get("properties") or {}).keys() if k != "official"]


async def _repair_doc_schema_with_assist(
    doc_key: str,
    doc: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Use assist_json(task='knowledge.repair_field', ...) to map unknown/synonym fields to valid schema fields."""
    warnings: list[dict[str, Any]] = []
    if not errors:
        return warnings

    schemas, _ = _load_schemas_and_registry()
    schema_obj = schemas.get(doc_key, {})

    bad_doc_fields: list[str] = []
    candidate_valid_props: list[str] = []
    path_valid_map: dict[str, list[str]] = {}
    for err in errors:
        err_path = str(err.get("path") or "/")
        valid_here = _schema_props_for_path(schema_obj, err_path)
        if not valid_here:
            valid_here = [k for k in (schema_obj.get("properties") or {}).keys() if k != "official"]
        path_valid_map[err_path] = valid_here
        for vp in valid_here:
            if vp not in candidate_valid_props:
                candidate_valid_props.append(vp)
        target_obj = _resolve_container_at_path(doc, err_path)
        if isinstance(target_obj, dict):
            for k in target_obj.keys():
                if k not in valid_here and str(k) not in bad_doc_fields:
                    bad_doc_fields.append(str(k))

    if not bad_doc_fields:
        return warnings

    difflib_suggestions = suggest_close_fields(bad_doc_fields, candidate_valid_props)

    res = await assist_json(
        task="knowledge.repair_field",
        instructions=(
            "将 JSON 文档中因拼写或同义词偏差未通过 schema 校验的字段名映射为对应 schema 中的合法字段名。"
            "参考 difflib_suggestions 中的候选建议，确认后返回 {\"mappings\": {\"原字段名\": \"合法字段名\"}}。"
        ),
        payload={
            "doc_key": doc_key,
            "errors": errors,
            "difflib_suggestions": difflib_suggestions,
            "schema_properties": candidate_valid_props,
        },
        schema=_REPAIR_FIELD_SCHEMA,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    mappings: dict[str, str] = {}
    if res.ok and isinstance(res.data, dict) and isinstance(res.data.get("mappings"), dict):
        mappings = {
            str(k): str(v)
            for k, v in res.data["mappings"].items()
            if k and v and str(v) != "official"
        }

    for err in errors:
        err_path = str(err.get("path") or "/")
        valid_here = path_valid_map.get(err_path) or candidate_valid_props
        target_obj = _resolve_container_at_path(doc, err_path)
        if not isinstance(target_obj, dict):
            continue
        for bad_k, good_k in list(mappings.items()):
            if bad_k in target_obj and bad_k not in valid_here and good_k in valid_here and good_k not in target_obj:
                target_obj[good_k] = target_obj.pop(bad_k)
                warnings.append(
                    {
                        "code": "field_repaired",
                        "path": f"{err_path.rstrip('/')}/{bad_k}",
                        "original_field": bad_k,
                        "repaired_to": good_k,
                        "message": f"repaired field {bad_k!r} -> {good_k!r} via knowledge.repair_field",
                    }
                )
    return warnings


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
    dry_run: bool = False,
    skip_access_check: bool = False,
) -> dict[str, Any]:
    """Execute the full PK.4 knowledge update pipeline: validate -> DB version -> write files."""
    if not skip_access_check:
        await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
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
                if dry_run:
                    latest = {
                        "version_no": 1,
                        "content": parsed,
                        "schema_valid": val_existing.ok,
                    }
                else:
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
        base_v_row = None
        if base_version is not None and hasattr(db_knowledge, "get_version"):
            try:
                base_v_row = await db_knowledge.get_version(
                    workspace_id, doc_key, base_version
                )
            except Exception:
                base_v_row = None
        base_v_content = (
            base_v_row.get("content") if isinstance(base_v_row, dict) else None
        )
        latest_content = latest.get("content") if latest else None
        return {
            "ok": False,
            "error": "version_conflict",
            "latest_version": latest_no,
            "latest_reason": latest.get("reason") if latest else None,
            "latest_doc": latest_content,
            "diff": diff_documents(base_v_content, latest_content),
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

    # Step 3: Repair & apply ops -> server fields -> validate schema & 3 hard invariants
    try:
        repaired_ops, repair_warnings = repair_knowledge_ops(doc_key, base_doc, ops)
        repaired_ops, assist_path_warnings = await _repair_ops_paths_with_assist(
            doc_key,
            base_doc,
            repaired_ops,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if assist_path_warnings:
            repair_warnings.extend(assist_path_warnings)
            repaired_ops, post_alias_warnings = repair_knowledge_ops(
                doc_key, base_doc, repaired_ops
            )
            repair_warnings.extend(post_alias_warnings)

        ev_ids = [
            str(r.get("evidence_id") or r.get("id"))
            for r in (evidence_refs or [])
            if isinstance(r, dict) and (r.get("evidence_id") or r.get("id"))
        ]

        new_doc, op_summaries = apply_ops(
            doc_key,
            base_doc,
            repaired_ops,
            reason=reason,
            now=now,
            evidence_ids=ev_ids or None,
        )
    except OpError as e:
        op_problems = [{"code": e.code, "path": e.path, "message": e.message}]
        schema_hint = _format_expected_structures(doc_key, op_problems)
        hint_suffix = f"\n\n{schema_hint}" if schema_hint else ""
        return {
            "ok": False,
            "dry_run": dry_run,
            "doc": doc_key,
            "error": e.code,
            "problems": op_problems,
            "expected_structures": schema_hint,
            "message": f"- {e.path}: {e.code} ({e.message}){hint_suffix}",
        }

    _normalize_legacy_aliases(doc_key, new_doc, old_doc=base_doc)
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
    has_hard_invariant_error = any(
        err.get("code")
        in (
            "append_only_violation",
            "official_is_publish_only",
            "target_needs_model_cell",
            "model_tier_1_forbidden",
            "model_needs_model_cell",
            "max_active_pillars_exceeded",
        )
        for err in val.errors
    )
    if not val.ok and not has_hard_invariant_error:
        assist_warnings = await _repair_doc_schema_with_assist(
            doc_key,
            new_doc,
            val.errors,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if assist_warnings:
            repair_warnings.extend(assist_warnings)
            _ensure_nested_ids_and_defaults(
                doc_key,
                new_doc,
                now=now,
                old_doc=valid_base_doc,
                evidence_ids=ev_ids or None,
            )
            val = validate_update(
                doc_key,
                valid_base_doc,
                new_doc,
                source=source,
                models_doc=models_doc,
                reason=reason,
            )

    if repair_warnings:
        if repaired_ops != ops:
            repair_warnings[0] = {**repair_warnings[0], "original_ops": ops}
        val.warnings = [*repair_warnings, *val.warnings]

    if not val.ok:
        schema_hint = _format_expected_structures(doc_key, val.errors)
        hint_suffix = f"\n\n{schema_hint}" if schema_hint else ""
        return {
            "ok": False,
            "dry_run": dry_run,
            "doc": doc_key,
            "error": val.errors[0]["code"],
            "problems": val.errors,
            "warnings": val.warnings,
            "expected_structures": schema_hint,
            "preview_doc": new_doc,
            "diff": diff_documents(base_doc or {}, new_doc),
            "message": f"Validation failed for {doc_key} (NOT SAVED):\n{val.format_problems()}{hint_suffix}",
        }

    # Step 4: Insert version into DB (or return preview when dry_run=True)
    _, content_sha = canonical_json(new_doc)
    if dry_run:
        diff_list = diff_documents(base_doc or {}, new_doc)
        expected_struct = _format_expected_structures(doc_key, [{"path": "/"}])
        ops_str = "; ".join(op_summaries[:5])
        if len(op_summaries) > 5:
            ops_str += f"; …等共 {len(op_summaries)} 条"
        diff_lines = [f"- {d['op']} {d['path']}" for d in diff_list[:20]]
        if len(diff_list) > 20:
            diff_lines.append(f"- ... and {len(diff_list) - 20} more diff(s)")
        diff_preview_str = "\n".join(diff_lines) if diff_lines else "(no changes)"
        return {
            "ok": True,
            "dry_run": True,
            "doc": doc_key,
            "base_version_no": latest_no,
            "would_be_version_no": (latest_no or 0) + 1,
            "preview_doc": new_doc,
            "content_sha256": content_sha,
            "diff": diff_list,
            "problems": [],
            "warnings": val.warnings,
            "expected_structures": expected_struct,
            "message": (
                f"Dry-run passed for {doc_key} (would save v{(latest_no or 0) + 1}): {ops_str}. "
                f"{len(diff_list)} diff change(s); no DB version or files written.\n"
                f"Diff preview:\n{diff_preview_str}\n\n{expected_struct}"
            ),
        }
    try:
        version_row = await db_knowledge.insert_version(
            user_id=user_id,
            workspace_id=workspace_id,
            doc_key=doc_key,
            base_version_no=base_version,
            content=new_doc,
            content_sha256=content_sha,
            ops=repaired_ops,
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
        curr_doc = vc.latest.get("content") if vc.latest else None
        return {
            "ok": False,
            "error": "version_conflict",
            "latest_version": curr_no,
            "latest_doc": curr_doc,
            "diff": diff_documents(base_doc, curr_doc),
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
    if len(op_summaries) > 5:
        ops_str += f"; …等共 {len(op_summaries)} 条"
    md_file = rendered_path(doc_key)
    render_str = f" Rendered {md_file}." if md_file else ""
    warn_str = ""
    if val.warnings or extra_notes:
        w_items = [
            f"{w['code']}: {w['message']}" for w in val.warnings[:5]
        ]
        if len(val.warnings) > 5:
            w_items.append(f"…等共 {len(val.warnings)} 条")
        all_w = w_items + extra_notes
        total_w = len(val.warnings) + len(extra_notes)
        warn_str = f" {total_w} warning(s): " + "; ".join(all_w)

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


_REQUIRED_PROP_RE = re.compile(r"'([^']+)' is a required property")
_ADDITIONAL_PROPS_RE = re.compile(r"Additional properties are not allowed \((.*)\)")

_VERDICT_MINIMAL_EXAMPLE = (
    '{"verdict":"NO_CHANGE","event":"<一句话事件>","rationale":"<定量依据>",'
    '"progress":"none","trigger":{"kind":"internal_material","ref":"<唤醒信号id>"},'
    '"next_validation":{"what":"<下一项看什么>","due":"2026-11-05"}}'
)


def _verdict_rejection_message(errors: list[dict[str, Any]]) -> str:
    """The verdict rejection the model can act on in one retry: unknown fields
    and missing fields by name, plus a minimal conforming example. The full
    expected-structure dump serves `knowledge_update`'s bigger documents; for a
    verdict it buried the two facts that fix the call."""
    unknown: list[str] = []
    missing: list[str] = []
    other: list[str] = []
    for err in errors:
        msg = str(err.get("message") or "")
        path = str(err.get("path") or "/")
        prefix = "" if path == "/" else path
        matched = _REQUIRED_PROP_RE.search(msg)
        if matched:
            missing.append(f"{prefix}/{matched.group(1)}")
            continue
        matched = _ADDITIONAL_PROPS_RE.search(msg)
        if matched:
            names = re.findall(r"'([^']+)'", matched.group(1))
            unknown.extend(f"{prefix}/{name}" for name in names)
            continue
        other.append(f"- {path}: {err.get('code')} ({msg})")
    lines: list[str] = []
    if unknown:
        lines.append("未知字段（schema 不认，删除后重试）：" + "、".join(unknown))
    if missing:
        lines.append("缺少必填字段（补上后重试）：" + "、".join(missing))
    if other:
        lines.append("其他问题：\n" + "\n".join(other))
    lines.append(f"最小合规示例：{_VERDICT_MINIMAL_EXAMPLE}")
    return "\n".join(lines)


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
    skip_access_check: bool = False,
) -> dict[str, Any]:
    """Validate and record an event verdict, update agenda, and render ``verdicts.md`` (PK.4)."""
    if not skip_access_check:
        await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
    v_copy = copy.deepcopy(verdict)
    consumed_signal_fields: list[str] = []
    for extra_sig_key in ("trigger_signal", "trigger_signals", "signal_id", "signal_ids", "signal_key", "signals"):
        if extra_sig_key in v_copy:
            sig_val = v_copy.pop(extra_sig_key, None)
            consumed_signal_fields.append(extra_sig_key)
            if extra_sig_key == "trigger_signal" and isinstance(sig_val, str) and sig_val.strip():
                trig_obj = v_copy.get("trigger")
                if isinstance(trig_obj, dict) and not trig_obj.get("ref"):
                    trig_obj["ref"] = sig_val.strip()
    # Temporary placeholders for schema check; real timestamp & ID are generated once in insert_verdict
    v_copy["at"] = "1970-01-01T00:00:00Z"
    had_id = bool(v_copy.get("id"))
    if not had_id:
        v_copy["id"] = "VD-19700101-01"

    thesis_row = await db_knowledge.latest_valid_version(workspace_id, "thesis")
    thesis_doc = thesis_row["content"] if thesis_row else None
    val = validate_verdict(v_copy, thesis_doc=thesis_doc)
    if consumed_signal_fields:
        val.warnings.append(
            {
                "code": "signal_fields_consumed",
                "path": "/",
                "dropped_fields": consumed_signal_fields,
                "message": f"这些字段已用于关闭信号，不存进结论: {', '.join(consumed_signal_fields)}",
            }
        )
    if not val.ok:
        return {
            "ok": False,
            "error": "schema_error",
            "problems": val.errors,
            "message": (
                "Invalid verdict payload (NOT SAVED):\n"
                f"{_verdict_rejection_message(val.errors)}"
            ),
        }

    v_copy.pop("at", None)
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
    raw_verdict_at = verdict_row.get("verdict_at")
    verdict_dt: datetime | None = None
    if isinstance(raw_verdict_at, datetime):
        verdict_dt = (
            raw_verdict_at.replace(tzinfo=timezone.utc)
            if raw_verdict_at.tzinfo is None
            else raw_verdict_at.astimezone(timezone.utc)
        )
        verdict_at_iso = verdict_dt.isoformat().replace("+00:00", "Z")
    elif isinstance(raw_verdict_at, str) and raw_verdict_at:
        verdict_at_iso = raw_verdict_at
        try:
            verdict_dt = datetime.fromisoformat(raw_verdict_at.replace("Z", "+00:00"))
        except ValueError:
            verdict_dt = None
    else:
        payload_at = (verdict_row.get("payload") or {}).get("at")
        verdict_at_iso = str(payload_at or "")

    # Automatically update agenda if an agenda document exists or agenda_ops were passed
    agenda_row = await db_knowledge.latest_version(workspace_id, "agenda")
    agenda_version_no: int | None = None
    agenda_error: str | None = None
    agenda_problems: list[dict[str, Any]] = []
    if agenda_row is not None or agenda_ops:
        combined_ops: list[dict[str, Any]] = []
        if agenda_row is not None:
            combined_ops.append(
                {
                    "op": "set",
                    "path": "/last_verdict",
                    "value": {
                        "verdict_id": verdict_id,
                        "word": word,
                        "at": verdict_at_iso,
                    },
                }
            )
            agenda_content = agenda_row.get("content") or {}
            # Mark matching validation as done only when trigger.ref equals its validation id
            val_id = (v_copy.get("trigger") or {}).get("ref")
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
                        "at": verdict_at_iso,
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
            base_version=agenda_row["version_no"] if agenda_row is not None else None,
            source="tool",
            backend=backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            now=verdict_dt,
            skip_access_check=skip_access_check,
        )
        if ag_res.get("ok"):
            agenda_version_no = ag_res.get("version_no")
        else:
            agenda_problems = list(ag_res.get("problems") or [])
            raw_ag_msg = str(ag_res.get("message") or ag_res.get("error") or "unknown error")
            if "Expected structure for " not in raw_ag_msg:
                ag_hint = _format_expected_structures(
                    "agenda",
                    agenda_problems or [{"path": "/", "code": "schema_error", "message": raw_ag_msg}],
                )
                if ag_hint:
                    raw_ag_msg = f"{raw_ag_msg}\n\n{ag_hint}"
            agenda_error = raw_ag_msg

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
    if agenda_version_no:
        ag_note = f"; updated agenda v{agenda_version_no}"
    elif agenda_error:
        ag_note = (
            "; agenda_update: failed (verdict saved, but agenda was NOT updated — "
            f"please retry with knowledge_update(doc=\"agenda\", ...)):\n{agenda_error}"
        )
    else:
        ag_note = ""
    msg = f"Recorded verdict {verdict_id} ({word} / {zh}){ag_note}. Rendered {VERDICTS_MD}.{warn_note}"
    result: dict[str, Any] = {
        "ok": True,
        "verdict_id": verdict_id,
        "word": word,
        "agenda_version_no": agenda_version_no,
        "warnings": val.warnings,
        "message": msg,
    }
    if agenda_error:
        result["agenda_update"] = "failed"
        result["agenda_error"] = agenda_error
        result["agenda_problems"] = agenda_problems
    elif agenda_version_no is not None:
        result["agenda_update"] = "updated"
    return result


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
    await verify_workspace_access(workspace_id, user_id)
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
