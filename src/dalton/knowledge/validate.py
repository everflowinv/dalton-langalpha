"""JSON Schema validation (PK.2) and hard invariants / soft warnings (PK.4)."""

from __future__ import annotations

import dataclasses
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from src.dalton.settings import get_dalton_config

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

SCHEMA_NAMES: tuple[str, ...] = (
    "common",
    "company",
    "thesis",
    "debate",
    "drivers",
    "management",
    "lessons",
    "agenda",
    "models",
    "verdict",
)

MAX_PROBLEMS = 10

_SOURCE_TYPE_EXPECTED_TIERS: dict[str, set[int]] = {
    "filing": {1},
    "company_release": {2},
    "broker_report": {3},
    "official_data": {4},
    "expert": {5},
    "news": {6},
    "model": {1, 2, 3},
    "internal_note": {3, 4, 5},
}


@dataclasses.dataclass
class ValidationResult:
    """Outcome of validating a knowledge document or update."""

    errors: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    warnings: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def format_problems(self, limit: int = MAX_PROBLEMS) -> str:
        lines: list[str] = []
        for err in self.errors[:limit]:
            lines.append(f"- {err['path']}: {err['code']} ({err['message']})")
        if len(self.errors) > limit:
            lines.append(f"- ... and {len(self.errors) - limit} more error(s)")
        return "\n".join(lines)

    def format_warnings(self, limit: int = MAX_PROBLEMS) -> str:
        lines: list[str] = []
        for w in self.warnings[:limit]:
            lines.append(f"{w['code']} at {w['path']}: {w['message']}")
        if len(self.warnings) > limit:
            lines.append(f"... and {len(self.warnings) - limit} more warning(s)")
        return "; ".join(lines)


@lru_cache(maxsize=1)
def _load_schemas_and_registry() -> tuple[dict[str, dict[str, Any]], Registry]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource]] = []
    for name in SCHEMA_NAMES:
        path = SCHEMAS_DIR / f"{name}.schema.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(raw)
        schemas[name] = raw
        uri = raw.get("$id", f"https://dalton.local/schemas/{name}")
        res = Resource.from_contents(raw)
        resources.append((uri, res))
    registry = Registry().with_resources(resources)
    return schemas, registry


def check_all_schemas() -> dict[str, str]:
    """Validate all 10 JSON Schemas against Draft 2020-12 and return name -> $id."""
    schemas, _ = _load_schemas_and_registry()
    return {name: s["$id"] for name, s in schemas.items()}


@lru_cache(maxsize=16)
def get_validator(schema_name: str) -> Draft202012Validator:
    """Return a cached Draft202012Validator with preloaded local Registry."""
    schemas, registry = _load_schemas_and_registry()
    if schema_name not in schemas:
        raise ValueError(f"Unknown schema {schema_name!r}")
    return Draft202012Validator(
        schemas[schema_name],
        registry=registry,
        format_checker=FormatChecker(),
    )


def validate_schema(schema_name: str, instance: Any) -> list[dict[str, Any]]:
    """Validate ``instance`` against ``<schema_name>.schema.json``."""
    validator = get_validator(schema_name)
    errors: list[dict[str, Any]] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        path = "/" + "/".join(str(p) for p in err.path) if err.path else "/"
        errors.append(
            {
                "code": "schema_error",
                "path": path,
                "message": err.message,
            }
        )
    return errors


def _check_hard_invariants(
    doc_key: str,
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any],
    *,
    source: str = "tool",
    models_doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Enforce the 3 hard invariants in PK.4:
    I1: thesis.evidence is append-only (old elements preserved verbatim in place).
    I2: thesis.official (and house_view.official_version) can only be written when source == 'publish'.
    I3: thesis.house_view.target requires model_ref pointing to a model cell (file/path, sheet, cell).
    """
    errors: list[dict[str, Any]] = []
    if doc_key != "thesis":
        return errors

    # I1: Append-only evidence ledger
    old_ev = (old_doc or {}).get("evidence") or (old_doc or {}).get("evidence_log") or []
    new_ev = new_doc.get("evidence") if "evidence" in new_doc else new_doc.get("evidence_log")
    if isinstance(old_ev, list) and old_ev:
        if not isinstance(new_ev, list) or len(new_ev) < len(old_ev):
            errors.append(
                {
                    "code": "append_only_violation",
                    "path": "/evidence",
                    "message": (
                        f"evidence ledger is append-only (had {len(old_ev)} entries, "
                        f"now has {len(new_ev) if isinstance(new_ev, list) else 0}); "
                        "do not delete entries — add a new entry with supersedes instead"
                    ),
                }
            )
        else:
            for idx, old_item in enumerate(old_ev):
                new_item = new_ev[idx]
                if old_item != new_item:
                    ev_id = (
                        old_item.get("id")
                        if isinstance(old_item, dict)
                        else f"index {idx}"
                    )
                    errors.append(
                        {
                            "code": "append_only_violation",
                            "path": f"/evidence/{idx}",
                            "message": (
                                f"append-only ({ev_id} was changed; "
                                "add a new entry with supersedes instead)"
                            ),
                        }
                    )

    # I2: official is publish-only
    if source != "publish":
        old_official = (old_doc or {}).get("official")
        new_official = new_doc.get("official")
        if new_official != old_official:
            errors.append(
                {
                    "code": "official_is_publish_only",
                    "path": "/official",
                    "message": "thesis.official can only be written by publish_report",
                }
            )
        old_hv_raw = (old_doc or {}).get("house_view")
        new_hv_raw = new_doc.get("house_view")
        old_hv_off = old_hv_raw.get("official_version") if isinstance(old_hv_raw, dict) else None
        new_hv_off = new_hv_raw.get("official_version") if isinstance(new_hv_raw, dict) else None
        if new_hv_off != old_hv_off:
            errors.append(
                {
                    "code": "official_is_publish_only",
                    "path": "/house_view/official_version",
                    "message": "house_view.official_version can only be written by publish_report",
                }
            )

    # I3: house_view.target requires model_ref pointing to a model cell
    hv = new_doc.get("house_view")
    if isinstance(hv, dict) and hv.get("target") is not None:
        target = hv.get("target")
        if not isinstance(target, dict):
            errors.append(
                {
                    "code": "target_needs_model_cell",
                    "path": "/house_view/target",
                    "message": "target must be an object with model_ref pointing to a model cell",
                }
            )
        else:
            mref = target.get("model_ref")
            if not isinstance(mref, dict):
                errors.append(
                    {
                        "code": "target_needs_model_cell",
                        "path": "/house_view/target/model_ref",
                        "message": "house_view.target requires model_ref (file/path, sheet, cell)",
                    }
                )
            else:
                kind = mref.get("kind")
                path = mref.get("path") or mref.get("file") or ""
                cell = mref.get("cell") or ""
                sheet = mref.get("sheet") or ""
                has_sheet_and_cell = bool(
                    (sheet and cell) or ("!" in str(cell) and len(str(cell).split("!")) == 2 and all(str(cell).split("!")))
                )
                if kind != "model_cell" or not path or not has_sheet_and_cell:
                    errors.append(
                        {
                            "code": "target_needs_model_cell",
                            "path": "/house_view/target/model_ref",
                            "message": (
                                "house_view.target.model_ref must have kind='model_cell', "
                                "path='model/<file>.xlsx', and cell='<Sheet>!<Cell>'"
                            ),
                        }
                    )
                elif models_doc is not None:
                    registered_paths = {
                        v.get("path")
                        for v in (models_doc.get("versions") or [])
                        if isinstance(v, dict)
                    }
                    if path not in registered_paths:
                        errors.append(
                            {
                                "code": "target_needs_model_cell",
                                "path": "/house_view/target/model_ref/path",
                                "message": (
                                    f"model file {path!r} is not registered in models.json versions "
                                    f"(registered: {sorted(p for p in registered_paths if p)})"
                                ),
                            }
                        )

    return errors


def _check_append_only_history_warning(
    old_list: list[Any],
    new_list: list[Any],
    path: str,
    warnings: list[dict[str, Any]],
    *,
    allow_actual_backfill: bool = False,
) -> None:
    if len(new_list) < len(old_list):
        warnings.append(
            {
                "code": "history_mutated",
                "path": path,
                "message": f"history array {path} shrank from {len(old_list)} to {len(new_list)} items",
            }
        )
        return
    for idx, old_item in enumerate(old_list):
        new_item = new_list[idx]
        if old_item == new_item:
            continue
        if (
            allow_actual_backfill
            and isinstance(old_item, dict)
            and isinstance(new_item, dict)
            and old_item.get("actual") is None
            and new_item.get("actual") is not None
        ):
            # Allowed one-time backfill of actual and outcome on guidance_history
            old_rest = {
                k: v
                for k, v in old_item.items()
                if k not in ("actual", "outcome", "delta_vs_mid_pct")
            }
            new_rest = {
                k: v
                for k, v in new_item.items()
                if k not in ("actual", "outcome", "delta_vs_mid_pct")
            }
            if old_rest == new_rest:
                continue
        warnings.append(
            {
                "code": "history_mutated",
                "path": f"{path}/{idx}",
                "message": f"existing entry at {path}/{idx} was modified",
            }
        )


def _check_soft_warnings(
    doc_key: str,
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any],
    *,
    reason: str = "",
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    if reason is not None and len(reason.strip()) < 4:
        warnings.append(
            {
                "code": "short_reason",
                "path": "/reason",
                "message": "reason is very short; write 1–2 sentences explaining why",
            }
        )

    raw_bytes = len(json.dumps(new_doc, ensure_ascii=False).encode("utf-8"))
    max_bytes = get_dalton_config().knowledge.max_doc_bytes
    if raw_bytes > max_bytes:
        warnings.append(
            {
                "code": "doc_size_exceeded",
                "path": "/",
                "message": (
                    f"document size ({raw_bytes} bytes) exceeds max_doc_bytes ({max_bytes}); "
                    "consider archiving older entries"
                ),
            }
        )

    if doc_key == "thesis":
        if old_doc:
            _check_append_only_history_warning(
                old_doc.get("house_view_history") or [],
                new_doc.get("house_view_history") or [],
                "/house_view_history",
                warnings,
            )
            old_pillars = {
                p["id"]: p
                for p in (old_doc.get("pillars") or [])
                if isinstance(p, dict) and "id" in p
            }
            new_pillars = {
                p["id"]: p
                for p in (new_doc.get("pillars") or [])
                if isinstance(p, dict) and "id" in p
            }
            for pid, old_p in old_pillars.items():
                if pid not in new_pillars:
                    warnings.append(
                        {
                            "code": "pillar_deleted",
                            "path": f"/pillars/{pid}",
                            "message": f"pillar {pid} was deleted; prefer op='retire' instead",
                        }
                    )
                else:
                    _check_append_only_history_warning(
                        old_p.get("status_history") or [],
                        new_pillars[pid].get("status_history") or [],
                        f"/pillars/{pid}/status_history",
                        warnings,
                    )

        agg = new_doc.get("aggregate_status")
        override = (new_doc.get("aggregate_override") or "").strip()
        for p in new_doc.get("pillars") or []:
            if not isinstance(p, dict):
                continue
            pid = p.get("id", "?")
            th = p.get("thresholds") or {}
            if not (th.get("break") or []):
                warnings.append(
                    {
                        "code": "missing_break_threshold",
                        "path": f"/pillars/{pid}/thresholds/break",
                        "message": f"pillar {pid} has no break threshold",
                    }
                )
            if (
                p.get("priority") == "core"
                and p.get("status") in ("impaired", "broken")
                and agg in ("strengthening", "intact")
                and not override
            ):
                warnings.append(
                    {
                        "code": "aggregate_status_mismatch",
                        "path": "/aggregate_status",
                        "message": (
                            f"aggregate_status is {agg!r} while core {pid} is {p.get('status')!r} "
                            "— set aggregate_status to watch/impaired/broken or add aggregate_override"
                        ),
                    }
                )

        for idx, ev in enumerate(new_doc.get("evidence") or []):
            if not isinstance(ev, dict):
                continue
            src = ev.get("source") or {}
            stype = src.get("type")
            stier = src.get("tier")
            expected = _SOURCE_TYPE_EXPECTED_TIERS.get(str(stype))
            if expected and isinstance(stier, int) and stier not in expected:
                warnings.append(
                    {
                        "code": "source_tier_mismatch",
                        "path": f"/evidence/{ev.get('id', idx)}/source/tier",
                        "message": f"source type {stype!r} usually has tier {sorted(expected)}, got {stier}",
                    }
                )

    elif doc_key == "debate" and old_doc:
        old_debates = {
            d["id"]: d
            for d in (old_doc.get("debates") or [])
            if isinstance(d, dict) and "id" in d
        }
        for d in new_doc.get("debates") or []:
            if isinstance(d, dict) and d.get("id") in old_debates:
                did = d["id"]
                _check_append_only_history_warning(
                    old_debates[did].get("history") or [],
                    d.get("history") or [],
                    f"/debates/{did}/history",
                    warnings,
                )

    elif doc_key == "management" and old_doc:
        _check_append_only_history_warning(
            old_doc.get("guidance_history") or [],
            new_doc.get("guidance_history") or [],
            "/guidance_history",
            warnings,
            allow_actual_backfill=True,
        )
        _check_append_only_history_warning(
            old_doc.get("credibility_notes") or [],
            new_doc.get("credibility_notes") or [],
            "/credibility_notes",
            warnings,
        )

    elif doc_key == "lessons" and old_doc:
        old_ids = {
            x["id"]
            for x in (old_doc.get("lessons") or [])
            if isinstance(x, dict) and "id" in x
        }
        new_ids = {
            x["id"]
            for x in (new_doc.get("lessons") or [])
            if isinstance(x, dict) and "id" in x
        }
        for deleted_id in sorted(old_ids - new_ids):
            warnings.append(
                {
                    "code": "lesson_deleted",
                    "path": f"/lessons/{deleted_ids if False else deleted_id}",
                    "message": f"lesson {deleted_id} was deleted; prefer op='retract' instead",
                }
            )

    elif doc_key == "agenda" and old_doc:
        old_qs = {
            q["id"]: q
            for q in (old_doc.get("open_questions") or [])
            if isinstance(q, dict) and "id" in q
        }
        for q in new_doc.get("open_questions") or []:
            if isinstance(q, dict) and q.get("id") in old_qs:
                qid = q["id"]
                _check_append_only_history_warning(
                    old_qs[qid].get("attempts") or [],
                    q.get("attempts") or [],
                    f"/open_questions/{qid}/attempts",
                    warnings,
                )

    elif doc_key == "models" and old_doc:
        _check_append_only_history_warning(
            old_doc.get("versions") or [],
            new_doc.get("versions") or [],
            "/versions",
            warnings,
        )

    return warnings


def validate_update(
    doc_key: str,
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any],
    *,
    source: str = "tool",
    models_doc: dict[str, Any] | None = None,
    reason: str = "updated",
) -> ValidationResult:
    """Validate ``new_doc`` against PK.2 schema, 3 hard invariants, and soft warning rules."""
    hard_errors = _check_hard_invariants(
        doc_key,
        old_doc,
        new_doc,
        source=source,
        models_doc=models_doc,
    )
    schema_errors = validate_schema(doc_key, new_doc)
    warnings = _check_soft_warnings(doc_key, old_doc, new_doc, reason=reason)
    return ValidationResult(
        errors=[*hard_errors, *schema_errors],
        warnings=warnings,
    )


def validate_verdict(
    verdict: dict[str, Any],
    *,
    thesis_doc: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate an event verdict against ``verdict.schema.json`` and warning rules."""
    schema_errors = validate_schema("verdict", verdict)
    warnings: list[dict[str, Any]] = []
    word = verdict.get("verdict")
    ev_ids = verdict.get("evidence_ids") or []
    if word and word != "NO_CHANGE" and not ev_ids:
        warnings.append(
            {
                "code": "verdict_missing_evidence",
                "path": "/evidence_ids",
                "message": f"verdict is {word} but evidence_ids is empty",
            }
        )
    if word == "THESIS_BROKEN" and isinstance(thesis_doc, dict):
        pillars = thesis_doc.get("pillars") or []
        has_broken = any(
            isinstance(p, dict) and p.get("status") in ("broken", "changed")
            for p in pillars
        )
        if not has_broken:
            warnings.append(
                {
                    "code": "broken_verdict_without_broken_pillar",
                    "path": "/verdict",
                    "message": "verdict is THESIS_BROKEN but no pillar in thesis.json has status broken or changed",
                }
            )
    return ValidationResult(errors=schema_errors, warnings=warnings)
