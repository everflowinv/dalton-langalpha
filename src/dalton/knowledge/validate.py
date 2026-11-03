"""JSON Schema validation (PK.2) and hard invariants / soft warnings (PK.4)."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
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
    "sector",
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
    "transcript": {2, 3},
    "broker_report": {3},
    "consensus": {3},
    "official_data": {4},
    "expert": {5},
    "news": {6},
    "model": {2, 3},
    "internal_note": {3, 4, 5},
    "legacy_thesis": {3, 4, 5},
    "market_color": {3, 4, 5},
}

_IMPLEMENTED_WAKE_KINDS: frozenset[str] = frozenset(
    {
        "filing",
        "earnings_window",
        "price_move",
        "consensus_change",
        "indicator",
        "due_validation",
        "pm_question",
        "internal_material",
        "date",
        "price_level",
    }
)


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
    """Validate every knowledge JSON Schema against Draft 2020-12 and return name -> $id.

    Includes the sector profile's ``sector`` schema (sector layer P1); the
    shared docs (thesis/debate/drivers/lessons/agenda/models/verdict) validate
    identically in both profiles — which doc set a workspace accepts is decided
    by ``knowledge/profiles.py``, never by relaxing these schemas.
    """
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


def _check_ranges_recursive(node: Any, path: str, errors: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if (
            "lo" in node
            and "hi" in node
            and isinstance(node.get("lo"), (int, float))
            and not isinstance(node.get("lo"), bool)
            and isinstance(node.get("hi"), (int, float))
            and not isinstance(node.get("hi"), bool)
            and set(node.keys()) <= {"lo", "hi", "unit"}
        ):
            if float(node["lo"]) > float(node["hi"]) + 1e-9:
                errors.append(
                    {
                        "code": "invalid_range",
                        "path": path or "/",
                        "message": f"区间下限 `lo`（{node['lo']}）不能大于上限 `hi`（{node['hi']}），请将 `lo` 改为小于或等于 `hi` 的数值",
                    }
                )
        for k, v in node.items():
            _check_ranges_recursive(v, f"{path}/{k}", errors)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _check_ranges_recursive(item, f"{path}/{idx}", errors)


def validate_schema(schema_name: str, instance: Any) -> list[dict[str, Any]]:
    """Validate ``instance`` against ``<schema_name>.schema.json``."""
    validator = get_validator(schema_name)
    errors: list[dict[str, Any]] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        path = "/" + "/".join(str(p) for p in err.path) if err.path else "/"
        code = "schema_error"
        if (
            schema_name == "agenda"
            and len(err.path) == 3
            and list(err.path)[0] == "wake_conditions"
            and list(err.path)[2] == "kind"
        ):
            code = "wake_kind_unimplemented"
        errors.append(
            {
                "code": code,
                "path": path,
                "message": err.message,
            }
        )
    if isinstance(instance, dict):
        _check_ranges_recursive(instance, "", errors)
    if schema_name == "thesis" and isinstance(instance, dict):
        pillars = instance.get("pillars")
        if isinstance(pillars, list):
            active_pillars = [
                p
                for p in pillars
                if isinstance(p, dict) and p.get("status") != "retired"
            ]
            if len(active_pillars) > 7:
                errors.append(
                    {
                        "code": "max_active_pillars_exceeded",
                        "path": "/pillars",
                        "message": (
                            f"非 retired 的支柱不超过 7 条（当前非 retired 支柱 {len(active_pillars)} 条，"
                            f"总支柱 {len(pillars)} 条）；请先 retire 旧支柱"
                        ),
                    }
                )
    if schema_name == "sector" and isinstance(instance, dict):
        # Codex r1 #4: duplicate member ids break the id-addressed op semantics
        # (`/members/M1/...` would hit whichever item sorts first) — reject in
        # every write path (tool update AND direct-edit reconcile).
        members = instance.get("members")
        if isinstance(members, list):
            seen_member_ids: set[str] = set()
            for idx, m in enumerate(members):
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mid = str(m["id"])
                if mid in seen_member_ids:
                    errors.append(
                        {
                            "code": "duplicate_member_id",
                            "path": f"/members/{idx}/id",
                            "message": (
                                f"成员 id {mid!r} 出现多于一次；成员按 id 寻址，"
                                "id 必须全表唯一"
                            ),
                        }
                    )
                else:
                    seen_member_ids.add(mid)
    if schema_name == "agenda" and isinstance(instance, dict):
        # `format: date-time` 没有注册 rfc3339-validator，FormatChecker 直接放
        # 行，schema 的 pattern 只挡形状——"2026-10-28T25:00:00-04:00" 这类
        # 形状合法但解析失败的值要在这里真实解析挡下，否则进库后验证项永不
        # 到期。到期探针读的三个日期字段（next_validations[].due、
        # open_questions[].due_date、wake_conditions[].params 的日期键，提示词
        # 第 1 批第 2 轮 #4）共用同一套 writable_due_error 口径。
        from src.dalton.autonomy.due import writable_due_error

        for idx, val_item in enumerate(instance.get("next_validations") or []):
            if not isinstance(val_item, dict) or "due" not in val_item:
                continue
            due_err = writable_due_error(val_item.get("due"))
            if due_err:
                errors.append(
                    {
                        "code": "invalid_due",
                        "path": f"/next_validations/{idx}/due",
                        "message": due_err,
                    }
                )
        for idx, q_item in enumerate(instance.get("open_questions") or []):
            if not isinstance(q_item, dict) or "due_date" not in q_item:
                continue
            due_err = writable_due_error(q_item.get("due_date"))
            if due_err:
                errors.append(
                    {
                        "code": "invalid_due",
                        "path": f"/open_questions/{idx}/due_date",
                        "message": due_err,
                    }
                )
        for idx, cond in enumerate(instance.get("wake_conditions") or []):
            if not isinstance(cond, dict):
                continue
            w_kind = cond.get("kind")
            if w_kind not in _IMPLEMENTED_WAKE_KINDS:
                if not any(
                    e.get("path") == f"/wake_conditions/{idx}/kind"
                    and e.get("code") == "wake_kind_unimplemented"
                    for e in errors
                ):
                    errors.append(
                        {
                            "code": "wake_kind_unimplemented",
                            "path": f"/wake_conditions/{idx}/kind",
                            "message": f"唤醒条件 `kind` 不支持 `{w_kind}`；请从 {sorted(_IMPLEMENTED_WAKE_KINDS)} 中选择",
                        }
                    )
                continue
            if w_kind == "price_level":
                pl = cond.get("price_level")
                uv = cond.get("underwriting_version")
                if not isinstance(pl, dict) or not isinstance(pl.get("band"), dict) or not pl.get("transition"):
                    errors.append(
                        {
                            "code": "price_level_wake_missing_fields",
                            "path": f"/wake_conditions/{idx}/price_level",
                            "message": "`kind` 为 `price_level` 时，必须填写 `price_level` 对象（包含 `band: {\"lo\": ..., \"hi\": ...}` 及 `transition`：`enter`、`exit` 或 `either`）",
                        }
                    )
                if not isinstance(uv, int) or isinstance(uv, bool) or uv < 1:
                    errors.append(
                        {
                            "code": "price_level_wake_missing_fields",
                            "path": f"/wake_conditions/{idx}/underwriting_version",
                            "message": "`kind` 为 `price_level` 时，必须填写 `underwriting_version`（大于等于 1 的整数，对应当前 `thesis.underwriting.scenario_version`）",
                        }
                    )
                continue
            if w_kind != "date":
                continue
            params = cond.get("params") if isinstance(cond.get("params"), dict) else {}
            date_keys = [key for key in ("date", "at", "due") if key in params]
            # kind=date 的唤醒条件必须至少带一个非空日期键（第 3 轮 #10）：
            # 没有日期的 date 条件到期探针永远读不到，写进来就是死条目。
            if not date_keys or not any(
                str(params.get(key) or "").strip() for key in date_keys
            ):
                errors.append(
                    {
                        "code": "missing_due",
                        "path": f"/wake_conditions/{idx}/params",
                        "message": (
                            "kind=\"date\" 的唤醒条件必须在 params.date（兼容 params.at / params.due）"
                            "写非空的到期时间，值为 YYYY-MM-DD 或带时区偏移的完整时刻"
                        ),
                    }
                )
                continue
            for key in date_keys:
                due_err = writable_due_error(params.get(key))
                if due_err:
                    errors.append(
                        {
                            "code": "invalid_due",
                            "path": f"/wake_conditions/{idx}/params/{key}",
                            "message": due_err,
                        }
                    )
    return errors


def _check_i3_target_issues(
    new_doc: dict[str, Any],
    models_doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    hv = new_doc.get("house_view")
    if not isinstance(hv, dict) or hv.get("target") is None:
        return issues
    target = hv.get("target")
    if not isinstance(target, dict):
        issues.append(
            {
                "code": "target_needs_model_cell",
                "path": "/house_view/target",
                "message": "target must be an object with model_ref pointing to a model cell",
            }
        )
        return issues
    mref = target.get("model_ref")
    if not isinstance(mref, dict):
        issues.append(
            {
                "code": "target_needs_model_cell",
                "path": "/house_view/target/model_ref",
                "message": "house_view.target requires model_ref (file/path, sheet, cell)",
            }
        )
        return issues
    kind = mref.get("kind")
    path = mref.get("path") or mref.get("file") or ""
    cell = mref.get("cell") or ""
    sheet = mref.get("sheet") or ""
    has_sheet_and_cell = bool(
        (sheet and cell)
        or (
            "!" in str(cell)
            and len(str(cell).split("!")) == 2
            and all(str(cell).split("!"))
        )
    )
    if kind != "model_cell" or not path or not has_sheet_and_cell:
        issues.append(
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
            issues.append(
                {
                    "code": "target_needs_model_cell",
                    "path": "/house_view/target/model_ref/path",
                    "message": (
                        f"model file {path!r} is not registered in models.json versions "
                        f"(registered: {sorted(p for p in registered_paths if p)})"
                    ),
                }
            )
    return issues


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
    # (§3.2 / §12.12: downgraded to warning when underwriting is present)
    has_underwriting = isinstance(new_doc.get("underwriting"), dict)
    if not has_underwriting:
        errors.extend(_check_i3_target_issues(new_doc, models_doc=models_doc))

    # I4: newly added model evidence must reference a model cell and cannot have tier 1
    # (§3.4: market_color evidence cannot back pillars or operating fact figures)
    old_ev_ids = {
        str(item.get("id"))
        for item in (old_ev if isinstance(old_ev, list) else [])
        if isinstance(item, dict) and item.get("id")
    }
    market_color_ev_ids: set[str] = set()
    ev_list = new_doc.get("evidence") if "evidence" in new_doc else new_doc.get("evidence_log")
    if isinstance(ev_list, list):
        # Old ids are exempt below, so a new entry must not borrow one. Count
        # against the old doc so duplicates already stored stay writable.
        allowed_ev_ids = Counter(
            str(item.get("id"))
            for item in (old_ev if isinstance(old_ev, list) else [])
            if isinstance(item, dict) and item.get("id")
        )
        seen_ev_ids: Counter[str] = Counter()
        for idx, ev in enumerate(ev_list):
            if not isinstance(ev, dict):
                continue
            ev_id = str(ev.get("id") or "")
            if ev_id:
                seen_ev_ids[ev_id] += 1
            if ev_id and seen_ev_ids[ev_id] > max(allowed_ev_ids[ev_id], 1):
                errors.append(
                    {
                        "code": "duplicate_evidence_id",
                        "path": f"/evidence/{idx}/id",
                        "message": f"evidence id {ev_id!r} appears more than once",
                    }
                )
                continue
            src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
            s_type = src.get("type")
            if s_type == "market_color" and ev_id:
                market_color_ev_ids.add(ev_id)
            if ev_id and ev_id in old_ev_ids:
                continue
            if s_type == "market_color":
                p_ids = [p for p in (ev.get("pillar_ids") or []) if p]
                bad_figs = [
                    f
                    for f in (ev.get("figures") or [])
                    if isinstance(f, dict) and f.get("label") in ("reported", "company_guidance")
                ]
                if p_ids or bad_figs:
                    errors.append(
                        {
                            "code": "market_color_pillar_forbidden",
                            "path": f"/evidence/{idx}/source/type",
                            "message": (
                                "`source.type` 为 `market_color` 的证据只能用于支撑 `market_view` 和 `expectations`，"
                                "不能绑定 `pillar_ids`，也不能包含 `label` 为 `reported` 或 `company_guidance` 的 `figures`；"
                                "请将 `pillar_ids` 置为空数组 `[]`，并移除上述 `figures`（或改用实际财报/公告来源类型）"
                            ),
                        }
                    )
                continue
            if s_type != "model":
                continue
            stier = src.get("tier")
            if stier == 1:
                errors.append(
                    {
                        "code": "model_tier_1_forbidden",
                        "path": f"/evidence/{idx}/source/tier",
                        "message": "evidence with source.type='model' cannot have tier=1 (default tier is 2–3)",
                    }
                )
            ref = src.get("ref")
            if not isinstance(ref, dict):
                errors.append(
                    {
                        "code": "model_needs_model_cell",
                        "path": f"/evidence/{idx}/source/ref",
                        "message": (
                            "evidence with source.type='model' must have ref pointing to a model cell "
                            "(kind='model_cell', path='model/<file>.xlsx', cell='<Sheet>!<Cell>')"
                        ),
                    }
                )
            else:
                r_kind = ref.get("kind")
                r_path = ref.get("path") or ref.get("file") or ""
                r_cell = ref.get("cell") or ""
                r_sheet = ref.get("sheet") or ""
                has_sheet_and_cell = bool(
                    (r_sheet and r_cell)
                    or (
                        "!" in str(r_cell)
                        and len(str(r_cell).split("!")) == 2
                        and all(str(r_cell).split("!"))
                    )
                )
                if r_kind != "model_cell" or not r_path or not has_sheet_and_cell:
                    errors.append(
                        {
                            "code": "model_needs_model_cell",
                            "path": f"/evidence/{idx}/source/ref",
                            "message": (
                                "evidence with source.type='model' must have ref with kind='model_cell', "
                                "path='model/<file>.xlsx', and cell='<Sheet>!<Cell>'"
                            ),
                        }
                    )

    if market_color_ev_ids:
        for p_idx, p in enumerate(new_doc.get("pillars") or []):
            if not isinstance(p, dict):
                continue
            for sh_idx, sh in enumerate(p.get("status_history") or []):
                if not isinstance(sh, dict):
                    continue
                cited_mc = [eid for eid in (sh.get("evidence_ids") or []) if str(eid) in market_color_ev_ids]
                if cited_mc:
                    errors.append(
                        {
                            "code": "market_color_pillar_forbidden",
                            "path": f"/pillars/{p_idx}/status_history/{sh_idx}/evidence_ids",
                            "message": f"支柱 `status_history[].evidence_ids` 不能引用 `source.type` 为 `market_color` 的证据 {cited_mc}；请移除该证据 ID 或改引基本面证据",
                        }
                    )
        uw_mc = new_doc.get("underwriting")
        if isinstance(uw_mc, dict):
            def _cited_mc_ids(refs: Any) -> list[str]:
                out: list[str] = []
                if isinstance(refs, str):
                    s = refs.strip()
                    if s in market_color_ev_ids:
                        out.append(s)
                elif isinstance(refs, dict):
                    for k in ("ref", "id", "evidence_id"):
                        v = refs.get(k)
                        if isinstance(v, str) and v.strip() in market_color_ev_ids:
                            out.append(v.strip())
                elif isinstance(refs, list):
                    for item in refs:
                        out.extend(_cited_mc_ids(item))
                return out

            anc = uw_mc.get("anchor")
            if isinstance(anc, dict):
                bad_anc = _cited_mc_ids(anc.get("source"))
                if bad_anc:
                    errors.append(
                        {
                            "code": "market_color_underwriting_forbidden",
                            "path": "/underwriting/anchor/source",
                            "message": f"`underwriting.anchor.source` 不得引用 `source.type` 为 `market_color` 的证据 {bad_anc}；请改填行情来源（如 `closing_price`）或非 `market_color` 证据",
                        }
                    )
            for st_idx, st in enumerate(uw_mc.get("states") or []):
                if not isinstance(st, dict):
                    continue
                calc = st.get("calc") if isinstance(st.get("calc"), dict) else {}
                bad_st = _cited_mc_ids(calc.get("sources")) + _cited_mc_ids(calc.get("ref"))
                if bad_st:
                    errors.append(
                        {
                            "code": "market_color_underwriting_forbidden",
                            "path": f"/underwriting/states/{st_idx}/calc",
                            "message": f"`underwriting.states[{st_idx}].calc` 不得引用 `source.type` 为 `market_color` 的证据 {bad_st}；请改引财报、研报或模型等证据",
                        }
                    )
            stress_mc = uw_mc.get("stress")
            if isinstance(stress_mc, dict) and isinstance(stress_mc.get("calc"), dict):
                bad_str = _cited_mc_ids(stress_mc["calc"].get("sources")) + _cited_mc_ids(stress_mc["calc"].get("ref"))
                if bad_str:
                    errors.append(
                        {
                            "code": "market_color_underwriting_forbidden",
                            "path": "/underwriting/stress/calc",
                            "message": f"`underwriting.stress.calc` 不得引用 `source.type` 为 `market_color` 的证据 {bad_str}；请改引财报、内部计算记录或模型等证据",
                        }
                    )
            wb_mc = uw_mc.get("weight_bounds")
            if isinstance(wb_mc, dict):
                bad_wb = _cited_mc_ids(wb_mc.get("sources"))
                if bad_wb:
                    errors.append(
                        {
                            "code": "market_color_underwriting_forbidden",
                            "path": "/underwriting/weight_bounds/sources",
                            "message": f"`underwriting.weight_bounds.sources` 不得引用 `source.type` 为 `market_color` 的证据 {bad_wb}；请改引非 `market_color` 的基本面或行业证据",
                        }
                    )
            bad_rr = _cited_mc_ids(uw_mc.get("rerating_sources"))
            if bad_rr:
                errors.append(
                    {
                        "code": "market_color_underwriting_forbidden",
                        "path": "/underwriting/rerating_sources",
                        "message": f"`underwriting.rerating_sources` 不得引用 `source.type` 为 `market_color` 的证据 {bad_rr}；请改引可验证估值重估已兑现的非 `market_color` 证据",
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
    models_doc: dict[str, Any] | None = None,
    debate_doc: dict[str, Any] | None = None,
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
            th_raw = p.get("thresholds")
            th = th_raw if isinstance(th_raw, dict) else {}
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

        # Only new entries: an append-only legacy entry cannot be corrected, so
        # warning on it every update is noise.
        legacy_ev_ids = {
            str(item.get("id"))
            for item in ((old_doc or {}).get("evidence") or [])
            if isinstance(item, dict) and item.get("id")
        }
        for idx, ev in enumerate(new_doc.get("evidence") or []):
            if not isinstance(ev, dict):
                continue
            if str(ev.get("id") or "") in legacy_ev_ids:
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

        if isinstance(new_doc.get("underwriting"), dict):
            hv_w = new_doc.get("house_view")
            if isinstance(hv_w, dict) and hv_w.get("target") is not None:
                i3_issues = _check_i3_target_issues(new_doc, models_doc=models_doc)
                if i3_issues:
                    warnings.extend(i3_issues)
                else:
                    warnings.append(
                        {
                            "code": "target_needs_model_cell",
                            "path": "/house_view/target",
                            "message": "已提供 `underwriting` 三情景估值时无需再填单点目标价 `house_view.target`，建议删除这个字段（它只接受对象，不能填 `null`）",
                        }
                    )
            from src.dalton.knowledge.scenario_math import (
                check_anchor_staleness,
                evaluate_underwriting,
                resolve_hurdles,
            )

            uw_w = new_doc["underwriting"]
            hurdles_w = resolve_hurdles(uw_w)
            stale_res = check_anchor_staleness(
                uw_w.get("anchor") if isinstance(uw_w.get("anchor"), dict) else None,
                now_date=None,
                anchor_stale_days=int(hurdles_w["anchor_stale_days"]),
                anchor_stale_move=hurdles_w["anchor_stale_move"],
            )
            if stale_res["is_stale"]:
                warnings.append(
                    {
                        "code": "anchor_stale",
                        "path": "/underwriting/anchor",
                        "message": f"`underwriting.anchor` 基准股价已过期或偏离现价过大（{'; '.join(stale_res['reasons'])}），请更新 `anchor`、重算 `states` 并递增 `scenario_version`",
                    }
                )
            eval_w = evaluate_underwriting(new_doc)
            for w_msg in eval_w.get("warnings") or []:
                if isinstance(w_msg, str) and w_msg.startswith("period_misaligned:"):
                    warnings.append(
                        {
                            "code": "period_misaligned",
                            "path": "/underwriting/states",
                            "message": f"`underwriting.states` 中 `eps_pe` 情景的 `calc.period` 不一致或与 `house_view.horizon_months` 窗口不对齐（{w_msg}）",
                        }
                    )
            if debate_doc is None:
                has_debate_refs = any(
                    isinstance(st, dict) and bool(st.get("debate_ids"))
                    for st in (uw_w.get("states") or [])
                ) or any(
                    isinstance(bv, dict) and bool(bv.get("debate_ids"))
                    for bv in (uw_w.get("breakeven_verdict") or [])
                )
                if has_debate_refs:
                    warnings.append(
                        {
                            "code": "debate_refs_unverified",
                            "path": "/underwriting/breakeven_verdict",
                            "message": "本次校验未加载 `debate` 文档，无法核对 `debate_ids` 是否存在；请确认所填 `D*` 已在 `debate` 文档中定义",
                        }
                    )
            if models_doc is None:
                has_model_refs = any(
                    isinstance(ev, dict)
                    and isinstance(ev.get("source"), dict)
                    and ev["source"].get("type") == "model"
                    for ev in (new_doc.get("evidence") or [])
                ) or any(
                    isinstance(st, dict)
                    and isinstance(st.get("calc"), dict)
                    and st["calc"].get("method") == "reference"
                    for st in (uw_w.get("states") or [])
                ) or (
                    isinstance(uw_w.get("stress"), dict)
                    and isinstance(uw_w["stress"].get("calc"), dict)
                    and uw_w["stress"]["calc"].get("method") == "reference"
                )
                if has_model_refs:
                    warnings.append(
                        {
                            "code": "model_refs_unverified",
                            "path": "/underwriting",
                            "message": "本次校验未加载 `models` 文档，无法核对模型引用是否已登记；请确认所引用的模型文件已在 `models` 文档中登记",
                        }
                    )

    elif doc_key == "debate":
        if old_doc:
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
        for idx, d in enumerate(new_doc.get("debates") or []):
            if not isinstance(d, dict):
                continue
            for e_idx, exp in enumerate(d.get("expectations") or []):
                if not isinstance(exp, dict):
                    continue
                active_ests = [
                    exp[k]
                    for k in ("street", "buyside", "house")
                    if isinstance(exp.get(k), dict) and exp[k].get("value") is not None
                ]
                bases = {str(e.get("basis") or "unknown") for e in active_ests}
                if len(active_ests) >= 2 and (len(bases) > 1 or "unknown" in bases):
                    warnings.append(
                        {
                            "code": "expectation_basis_mismatch",
                            "path": f"/debates/{idx}/expectations/{e_idx}",
                            "message": (
                                f"同一 `expectations` 条目下 `street`、`buyside`、`house` 的口径 `basis`（{sorted(bases)}）不同或含 `unknown`，"
                                "页面将显示“口径不同，未比较”，请保持 `gap` 为 `null`"
                            ),
                        }
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
                    "path": f"/lessons/{deleted_id}",
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


def _is_v2_doc(doc_key: str, doc: dict[str, Any]) -> bool:
    if not isinstance(doc, dict):
        return False
    from src.dalton.knowledge.ops import _has_non_empty_v2_value

    if doc.get("schema_version") == 2:
        return True
    if doc_key == "thesis":
        if _has_non_empty_v2_value(doc.get("underwriting")):
            return True
        hv = doc.get("house_view")
        if isinstance(hv, dict) and any(
            _has_non_empty_v2_value(hv.get(k))
            for k in (
                "actionability",
                "binding_constraint",
                "binding_constraint_text",
                "flip_conditions",
                "entry_band",
                "trigger_refs",
                "missing",
                "review_by",
            )
        ):
            return True
        mv = doc.get("market_view")
        if isinstance(mv, dict) and any(
            _has_non_empty_v2_value(mv.get(k))
            for k in ("observations", "positioning", "positioning_sources")
        ):
            return True
    elif doc_key == "debate":
        for d in doc.get("debates") or []:
            if isinstance(d, dict) and any(
                _has_non_empty_v2_value(d.get(k)) for k in ("transmission", "expectations")
            ):
                return True
    elif doc_key == "drivers":
        for drv in doc.get("drivers") or []:
            if isinstance(drv, dict) and _has_non_empty_v2_value(drv.get("response")):
                return True
    elif doc_key == "agenda":
        watch = doc.get("watch")
        if isinstance(watch, dict):
            for ev in watch.get("events") or []:
                if isinstance(ev, dict) and _has_non_empty_v2_value(ev.get("review_by")):
                    return True
        for wc in doc.get("wake_conditions") or []:
            if isinstance(wc, dict) and (
                wc.get("kind") == "price_level"
                or any(
                    _has_non_empty_v2_value(wc.get(k))
                    for k in ("price_level", "direction", "transmission", "underwriting_version")
                )
            ):
                return True
    return False


def _extract_protection_info(prot: Any) -> dict[str, Any]:
    if not isinstance(prot, dict):
        return {
            "complete": False,
            "needs_financing": None,
            "surv_text": "",
            "value_range": None,
            "missing": ["protection"],
        }
    surv = prot.get("survival")
    if isinstance(surv, dict):
        surv_text = str(surv.get("text") or "").strip()
        nf = surv.get("needs_financing") or prot.get("needs_financing")
    else:
        surv_text = str(surv or "").strip()
        nf = prot.get("needs_financing")

    val = prot.get("value")
    if isinstance(val, dict):
        val_text = str(val.get("text") or val.get("mechanism") or "").strip()
        v_range = val.get("value_range") or prot.get("value_range")
    else:
        val_text = str(val or "").strip()
        v_range = prot.get("value_range")

    path_text = str(prot.get("path") or "").strip()
    fail_text = str(prot.get("failure") or "").strip()

    missing: list[str] = []
    if not surv_text:
        missing.append("survival")
    if nf not in ("yes", "no", "unknown"):
        missing.append("needs_financing")
    if not val_text:
        missing.append("value")
    if not isinstance(v_range, dict) or "lo" not in v_range or "hi" not in v_range:
        missing.append("value_range")
    if not path_text:
        missing.append("path")
    if not fail_text:
        missing.append("failure")

    return {
        "complete": len(missing) == 0,
        "needs_financing": nf,
        "surv_text": surv_text,
        "value_range": v_range,
        "missing": missing,
    }


def _parse_cmp_timestamp(val: Any) -> str | None:
    if not isinstance(val, str) or not val.strip():
        return None
    from datetime import datetime, timezone

    s = val.strip()
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return f"{s}T23:59:59+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _parse_sheet_cell(sheet_cell: str) -> tuple[str, str] | None:
    import re

    if "!" not in sheet_cell:
        return None
    sh, cl = sheet_cell.split("!", 1)
    sh = sh.strip().strip("'")
    cl = cl.strip()
    if not sh or not cl:
        return None
    if not re.match(r"^[$]?[A-Z]{1,3}[$]?\d+(?::[$]?[A-Z]{1,3}[$]?\d+)?$", cl):
        return None
    return sh, cl


def _is_calculation_evidence(ev: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    """Return ``(is_calc, model_path, sheet_name)`` for an evidence entry (§3.3)."""
    import re

    src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
    s_type = src.get("type")
    s_ref = src.get("ref") if isinstance(src.get("ref"), dict) else {}
    fact_txt = str(ev.get("fact") or "").strip()
    figs = ev.get("figures") if isinstance(ev.get("figures"), list) else []

    if s_type == "model":
        if s_ref.get("kind") != "model_cell":
            return False, None, None
        m_path = str(s_ref.get("path") or s_ref.get("file") or "").strip()
        sc = _parse_sheet_cell(str(s_ref.get("cell") or ""))
        if not m_path or sc is None:
            return False, None, None
        return True, m_path, sc[0]

    if s_type == "internal_note":
        if s_ref.get("kind") == "model_cell":
            m_path = str(s_ref.get("path") or s_ref.get("file") or "").strip()
            sc = _parse_sheet_cell(str(s_ref.get("cell") or ""))
            if m_path and sc is not None:
                return True, m_path, sc[0]
        if any(neg in fact_txt for neg in ("尚无公式", "无公式", "待研究", "未计算")):
            return False, None, None
        has_valid_figs = any(
            isinstance(f, dict)
            and isinstance(f.get("value"), (int, float))
            and not isinstance(f.get("value"), bool)
            and str(f.get("metric") or "").strip()
            and str(f.get("unit") or "").strip()
            and str(f.get("period") or "").strip()
            for f in figs
        )
        has_formula_and_io = bool(
            "=" in fact_txt
            and re.search(r"\d+(?:\.\d+)?\s*(?:[×*/+-]|x\b)", fact_txt)
            and re.search(r"=\s*[^\d\n]*\d+(?:\.\d+)?", fact_txt)
        )
        if has_formula_and_io and (
            has_valid_figs or bool(str(ev.get("period") or "").strip())
        ):
            return True, None, None
    return False, None, None


def _is_resolvable_calc_ref(
    ref: Any,
    new_doc: dict[str, Any],
    models_doc: dict[str, Any] | None = None,
) -> bool:
    """Verify that ``calc.ref`` resolves to an existing frozen calculation record (§3.3).

    A frozen calculation record is either:
    1. An evidence entry in ``new_doc["evidence"]`` that is a true calculation record
       (``source.type == "model"`` with ``model_cell`` backed by a registered model when
       ``models_doc`` is provided, or ``source.type == "internal_note"`` with explicit
       formula, numeric inputs, and numeric output — also accepting
       ``E-xxxx#Sheet!Cell`` composite syntax), or
    2. A registered model in ``models_doc["versions"]`` with a valid ``Sheet!Cell``
       reference whose sheet matches the registered sheets/key_outputs when present.
       When ``models_doc is None``, model registration cannot be verified and a soft
       warning is emitted instead.
    """
    calc_ev_map: dict[str, tuple[str | None, str | None]] = {}
    registered_paths: set[str] = set()
    registered_versions: dict[str, str] = {}
    known_sheets_by_path: dict[str, set[str]] = {}
    has_models_doc = isinstance(models_doc, dict)

    if has_models_doc:
        for v in models_doc.get("versions") or []:
            if not isinstance(v, dict):
                continue
            v_path = str(v.get("path") or "").strip()
            v_id = str(v.get("id") or v.get("version") or "").strip()
            if v_path:
                registered_paths.add(v_path)
                sheets_set = known_sheets_by_path.setdefault(v_path, set())
                raw_sheets = v.get("sheets") or v.get("sheet_names")
                if isinstance(raw_sheets, list):
                    for sh_item in raw_sheets:
                        if isinstance(sh_item, str) and sh_item.strip():
                            sheets_set.add(sh_item.strip())
                        elif isinstance(sh_item, dict) and sh_item.get("name"):
                            sheets_set.add(str(sh_item["name"]).strip())
                elif isinstance(raw_sheets, dict):
                    for sh_k in raw_sheets:
                        if str(sh_k).strip():
                            sheets_set.add(str(sh_k).strip())
                raw_kos = v.get("key_outputs")
                if isinstance(raw_kos, list):
                    for ko in raw_kos:
                        if isinstance(ko, dict):
                            sc = _parse_sheet_cell(str(ko.get("cell") or ""))
                            if sc is not None:
                                sheets_set.add(sc[0])
                            elif ko.get("sheet"):
                                sheets_set.add(str(ko["sheet"]).strip())
                elif isinstance(raw_kos, dict):
                    for ko in raw_kos.values():
                        if isinstance(ko, dict):
                            sc = _parse_sheet_cell(str(ko.get("cell") or ""))
                            if sc is not None:
                                sheets_set.add(sc[0])
                            elif ko.get("sheet"):
                                sheets_set.add(str(ko["sheet"]).strip())
            if v_id and v_path:
                registered_versions[v_id] = v_path

    for ev in new_doc.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        ev_id = str(ev.get("id") or "").strip()
        if not ev_id:
            continue
        is_calc, m_path, ev_sheet = _is_calculation_evidence(ev)
        if not is_calc:
            continue
        if m_path is not None and has_models_doc:
            if m_path not in registered_paths:
                continue
            reg_sheets = known_sheets_by_path.get(m_path) or set()
            if reg_sheets and ev_sheet and ev_sheet not in reg_sheets:
                continue
        calc_ev_map[ev_id] = (m_path, ev_sheet)

    def _check_str_ref(s: str) -> bool:
        s = s.strip()
        if not s:
            return False
        if s in calc_ev_map:
            return True
        if "#" in s:
            prefix, sc_part = s.split("#", 1)
            prefix = prefix.strip()
            sc = _parse_sheet_cell(sc_part.strip())
            if sc is None:
                return False
            sheet_name, _ = sc
            if prefix in calc_ev_map:
                m_path, _ = calc_ev_map[prefix]
                if m_path and has_models_doc:
                    reg_sheets = known_sheets_by_path.get(m_path) or set()
                    if reg_sheets and sheet_name not in reg_sheets:
                        return False
                return True
            if has_models_doc:
                m_path = registered_versions.get(prefix, prefix)
                if m_path in registered_paths:
                    reg_sheets = known_sheets_by_path.get(m_path) or set()
                    if reg_sheets and sheet_name not in reg_sheets:
                        return False
                    return True
                return False
            if prefix.startswith("model/") or prefix.endswith((".xlsx", ".xls", ".xlsm")):
                return True
        return False

    if isinstance(ref, str):
        return _check_str_ref(ref)

    if isinstance(ref, dict):
        rid = str(ref.get("id") or ref.get("ref") or ref.get("evidence_id") or "").strip()
        rpath = str(ref.get("path") or ref.get("file") or "").strip()
        rcell = str(ref.get("cell") or "").strip()
        rsheet = str(ref.get("sheet") or "").strip()
        sc_str = rcell if "!" in rcell else (f"{rsheet}!{rcell}" if rsheet and rcell else "")
        if rid:
            if sc_str and "#" not in rid:
                return _check_str_ref(f"{rid}#{sc_str}")
            return _check_str_ref(rid)
        if rpath and sc_str:
            return _check_str_ref(f"{rpath}#{sc_str}")
    return False


def _collect_dated_agenda_and_pillar_refs(
    thesis_doc: dict[str, Any],
    agenda_doc: dict[str, Any] | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Return ``(dated_all_ids, dated_validation_items)`` from ``agenda_doc`` (or embedded ``agenda``).

    - ``dated_validation_items`` contains ONLY dated ``next_validations`` entries (used for ``no_view`` §12.4).
    - ``dated_all_ids`` contains all dated ``agenda`` entries (``next_validations``, ``watch.events``,
      ``open_questions``, and ``kind=="date"`` ``wake_conditions`` reading ``params.date/at/due`` or
      ``date.at`` per ``agenda.schema.json``) plus ``P*`` pillars whose ``next_proof_point.validation_id``
      points to a dated ``agenda`` item (used for ``wait_evidence`` ``trigger_refs``).
    """
    from src.dalton.knowledge.scenario_math import _parse_iso_date

    dated_all_ids: set[str] = set()
    dated_validation_items: list[tuple[str, Any]] = []

    ag_src = (
        agenda_doc
        if isinstance(agenda_doc, dict)
        else (thesis_doc.get("agenda") if isinstance(thesis_doc.get("agenda"), dict) else None)
    )
    if isinstance(ag_src, dict):
        for v in ag_src.get("next_validations") or []:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("id") or "").strip()
            due_dt = _parse_iso_date(v.get("due") or v.get("review_by") or v.get("date"))
            if vid and due_dt is not None:
                dated_all_ids.add(vid)
                dated_validation_items.append((vid, due_dt))
        watch = ag_src.get("watch")
        if isinstance(watch, dict):
            for ev in watch.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                wid = str(ev.get("id") or "").strip()
                due_dt = _parse_iso_date(ev.get("date") or ev.get("review_by") or ev.get("due"))
                if wid and due_dt is not None:
                    dated_all_ids.add(wid)
        for q in ag_src.get("open_questions") or []:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "").strip()
            due_dt = _parse_iso_date(q.get("due_date") or q.get("due") or q.get("review_by"))
            if qid and due_dt is not None:
                dated_all_ids.add(qid)
        for wc in ag_src.get("wake_conditions") or []:
            if not isinstance(wc, dict):
                continue
            cid = str(wc.get("id") or "").strip()
            if wc.get("kind") != "date":
                continue
            wc_params = wc.get("params") if isinstance(wc.get("params"), dict) else {}
            wc_date_obj = wc.get("date") if isinstance(wc.get("date"), dict) else {}
            raw_wc_dt = (
                wc_params.get("date")
                or wc_params.get("at")
                or wc_params.get("due")
                or wc_date_obj.get("at")
            )
            due_dt = _parse_iso_date(raw_wc_dt)
            if cid and due_dt is not None:
                dated_all_ids.add(cid)

        for p in thesis_doc.get("pillars") or []:
            if not isinstance(p, dict):
                continue
            p_id = str(p.get("id") or "").strip()
            npp = p.get("next_proof_point")
            if isinstance(npp, dict) and p_id:
                v_id = str(npp.get("validation_id") or "").strip()
                if v_id and v_id in dated_all_ids:
                    dated_all_ids.add(p_id)

    return dated_all_ids, dated_validation_items


def _check_v2_semantics(
    doc_key: str,
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any],
    *,
    models_doc: dict[str, Any] | None = None,
    agenda_doc: dict[str, Any] | None = None,
    debate_doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Enforce v2 semantic rules, §12.4 consistency matrix, §12.5 protection, and §12.6 frozen expectations."""
    errors: list[dict[str, Any]] = []
    if not isinstance(new_doc, dict) or not (
        _is_v2_doc(doc_key, new_doc) or (isinstance(old_doc, dict) and _is_v2_doc(doc_key, old_doc))
    ):
        return errors

    if doc_key == "thesis":
        # 1. §3.1 market_view checks
        mv = new_doc.get("market_view")
        if isinstance(mv, dict):
            summary = mv.get("summary")
            if isinstance(summary, str) and len(summary) > 200:
                errors.append(
                    {
                        "code": "market_view_summary_too_long",
                        "path": "/market_view/summary",
                        "message": f"`market_view.summary` 长度不得超过 200 字（当前 {len(summary)} 字），请压缩字数",
                    }
                )
            for idx, obs in enumerate(mv.get("observations") or []):
                if isinstance(obs, dict) and not (obs.get("sources") or []):
                    errors.append(
                        {
                            "code": "market_observation_missing_sources",
                            "path": f"/market_view/observations/{idx}/sources",
                            "message": "`market_view.observations[].sources` 必须至少包含一条来源引用",
                        }
                    )
            pos = mv.get("positioning")
            if pos in ("crowded_long", "balanced", "crowded_short") and not (
                mv.get("positioning_sources") or []
            ):
                errors.append(
                    {
                        "code": "positioning_missing_sources",
                        "path": "/market_view/positioning_sources",
                        "message": f"`market_view.positioning` 为 `{pos}` 时，`market_view.positioning_sources` 必须至少有一条来源引用；若无来源证据，请将 `market_view.positioning` 改为 `unknown`",
                    }
                )

        # 2. §2.1–§2.5, §3.2, §3.3, §12.1–§12.5, §12.7 house_view & underwriting checks
        hv = new_doc.get("house_view") if isinstance(new_doc.get("house_view"), dict) else {}
        stance = str(hv.get("stance") or "no_view")
        conviction = str(hv.get("conviction") or "none")
        actionability = hv.get("actionability")
        bc = hv.get("binding_constraint")
        bc_text = str(hv.get("binding_constraint_text") or "").strip()
        flip = hv.get("flip_conditions") if isinstance(hv.get("flip_conditions"), dict) else {}
        upgrade_if = str(flip.get("upgrade_if") or "").strip()
        downgrade_if = str(flip.get("downgrade_if") or "").strip()
        uw = new_doc.get("underwriting") if isinstance(new_doc.get("underwriting"), dict) else None
        dated_all_ids, dated_validation_items = _collect_dated_agenda_and_pillar_refs(
            new_doc, agenda_doc=agenda_doc
        )

        if stance in ("long", "short", "neutral"):
            if (
                not isinstance(mv, dict)
                or not str(mv.get("summary") or "").strip()
                or not (mv.get("observations") or [])
            ):
                errors.append(
                    {
                        "code": "missing_market_view",
                        "path": "/market_view",
                        "message": f"`house_view.stance` 为 `{stance}`（非 `no_view`）时，必须填写包含非空 `summary` 与非空 `observations` 的 `market_view`",
                    }
                )
            if conviction == "none":
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/house_view/conviction",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`house_view.conviction` 不得为 `none`（请填 `high`、`medium` 或 `low`）",
                    }
                )
            hm = hv.get("horizon_months")
            if not isinstance(hm, int) or isinstance(hm, bool) or not (1 <= hm <= 60):
                errors.append(
                    {
                        "code": "missing_horizon_months",
                        "path": "/house_view/horizon_months",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`house_view.horizon_months` 必填（1–60 的整数）",
                    }
                )
            if not actionability:
                errors.append(
                    {
                        "code": "missing_actionability",
                        "path": "/house_view/actionability",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`house_view.actionability` 必填（`act_now`、`wait_price`、`wait_evidence` 或 `pass`）",
                    }
                )
            if not bc:
                errors.append(
                    {
                        "code": "missing_binding_constraint",
                        "path": "/house_view/binding_constraint",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`house_view.binding_constraint` 必填（`none`、`price`、`evidence`、`timing`、`downside_path` 或 `implementation`）",
                    }
                )
            if uw is None:
                errors.append(
                    {
                        "code": "missing_underwriting",
                        "path": "/underwriting",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`underwriting` 必填（含 `anchor`、`scenario_version`、`states`、`protection`、`breakeven_verdict`）",
                    }
                )
            if not isinstance(hv.get("flip_conditions"), dict) or not upgrade_if or not downgrade_if:
                errors.append(
                    {
                        "code": "missing_flip_conditions",
                        "path": "/house_view/flip_conditions",
                        "message": f"`house_view.stance` 为 `{stance}`（非 `no_view`）时，`house_view.flip_conditions` 必须同时填写非空的 `upgrade_if` 与 `downgrade_if`",
                    }
                )

        elif stance == "no_view":
            if actionability != "pass":
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/house_view/actionability",
                        "message": "`house_view.stance` 为 `no_view` 时，`house_view.actionability` 必须设为 `pass`",
                    }
                )
            missing_list = [str(m).strip() for m in (hv.get("missing") or []) if str(m).strip()]
            review_by = str(hv.get("review_by") or "").strip()
            if not missing_list or not review_by:
                errors.append(
                    {
                        "code": "no_view_missing_fields",
                        "path": "/house_view",
                        "message": "`house_view.stance` 为 `no_view` 时，必须填写非空的 `house_view.missing` 与 `house_view.review_by`",
                    }
                )
            elif review_by:
                from src.dalton.knowledge.scenario_math import _parse_iso_date

                rb_date = _parse_iso_date(review_by)
                ref_date = _parse_iso_date(new_doc.get("as_of"))
                if rb_date is None or (
                    ref_date is not None and not (0 <= (rb_date - ref_date).days <= 30)
                ):
                    errors.append(
                        {
                            "code": "no_view_review_by_too_far",
                            "path": "/house_view/review_by",
                            "message": "`house_view.stance` 为 `no_view` 时，`house_view.review_by` 必须是距离文档 `as_of` 30 天内的日期（`YYYY-MM-DD`）",
                        }
                    )
                has_within_30d_val = any(
                    ref_date is None or (0 <= (v_dt - ref_date).days <= 30)
                    for _, v_dt in dated_validation_items
                )
                if not has_within_30d_val:
                    errors.append(
                        {
                            "code": "no_view_missing_agenda_validation",
                            "path": "/house_view/review_by",
                            "message": (
                                "`house_view.stance` 为 `no_view` 时，`agenda.next_validations` 中必须至少有一条 "
                                "`due` 在文档 `as_of` 起 30 天内的验证项；请先在 `agenda` 文档的 `next_validations` 中添加该验证项"
                            ),
                        }
                    )

        # §2.4 & §12.4 binding_constraint & binding_constraint_text rules
        if bc is not None:
            if bc == "none":
                if stance == "neutral" or actionability != "act_now":
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/binding_constraint",
                            "message": "仅当 `house_view.stance` 为 `long` 或 `short` 且 `actionability` 为 `act_now` 时，`binding_constraint` 才能填 `none`；其余情况请从 `price`、`evidence`、`timing`、`downside_path`、`implementation` 中选择",
                        }
                    )
            elif not bc_text:
                errors.append(
                    {
                        "code": "missing_binding_constraint_text",
                        "path": "/house_view/binding_constraint_text",
                        "message": f"`house_view.binding_constraint` 为 `{bc}`（非 `none`）时，`house_view.binding_constraint_text` 必须填写具体的制约原因与量化条件",
                    }
                )

        # §2.3 & §12.4 actionability-specific checks
        if actionability == "act_now":
            if stance not in ("long", "short"):
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/house_view/actionability",
                        "message": f"`house_view.actionability` 为 `act_now` 时，`house_view.stance` 必须为 `long` 或 `short`（当前为 `{stance}`）",
                    }
                )
        elif actionability == "wait_price":
            if stance not in ("long", "short", "neutral"):
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/house_view/actionability",
                        "message": f"`house_view.actionability` 为 `wait_price` 时，`house_view.stance` 必须为 `long`、`short` 或 `neutral`（当前为 `{stance}`）",
                    }
                )
            if not isinstance(hv.get("entry_band"), dict):
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/house_view/entry_band",
                        "message": "`house_view.actionability` 为 `wait_price` 时，必须填写 `house_view.entry_band`（`{\"lo\": ..., \"hi\": ..., \"unit\": ...}`）",
                    }
                )
        elif actionability == "wait_evidence":
            trig_refs = [str(t).strip() for t in (hv.get("trigger_refs") or []) if str(t).strip()]
            if not trig_refs:
                errors.append(
                    {
                        "code": "wait_evidence_missing_triggers",
                        "path": "/house_view/trigger_refs",
                        "message": "`house_view.actionability` 为 `wait_evidence` 时，`house_view.trigger_refs` 不能为空，必须填入 `agenda` 中带日期的条目 ID（`V-xxx`、`W*`、`Q-xxx`、`C*`，或关联了上述条目的 `P*`）",
                    }
                )
            else:
                import re

                invalid_refs: list[str] = []
                dated_refs: list[str] = []
                for tr in trig_refs:
                    if not re.match(r"^(V-\d{3}|W\d+|Q-\d{3}|C\d+|P\d+)$", tr):
                        invalid_refs.append(tr)
                        continue
                    if tr not in dated_all_ids:
                        invalid_refs.append(tr)
                    else:
                        dated_refs.append(tr)
                if invalid_refs or not dated_refs:
                    errors.append(
                        {
                            "code": "wait_evidence_invalid_triggers",
                            "path": "/house_view/trigger_refs",
                            "message": (
                                f"`house_view.actionability` 为 `wait_evidence` 时，`trigger_refs` 必须指向 `agenda` 文档中真实存在且带日期的条目 "
                                f"（`V-xxx`、`W*`、`Q-xxx`、`kind` 为 `date` 的 `C*`，或 `next_proof_point.validation_id` 指向上述 `agenda` 条目的 `P*`；"
                                f"以下引用未在 `agenda` 中找到或缺少日期：{invalid_refs or trig_refs}）"
                            ),
                        }
                    )

        # §12.4 Row 7: conviction == "high" checks against core pillars
        if conviction == "high":
            for p in new_doc.get("pillars") or []:
                if (
                    isinstance(p, dict)
                    and p.get("priority") == "core"
                    and p.get("status") in ("impaired", "broken")
                ):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/conviction",
                            "message": f"`priority` 为 `core` 的核心支柱 `{p.get('id')}` 状态为 `{p.get('status')}` 时，`house_view.conviction` 不得为 `high`；请将 `conviction` 改为 `medium` 或 `low`",
                        }
                    )

        # Underwriting math & consistency matrix checks
        if uw is not None:
            from src.dalton.knowledge.scenario_math import (
                evaluate_underwriting,
            )

            eval_res = evaluate_underwriting(new_doc)
            for math_err in eval_res["errors"]:
                errors.append(
                    {
                        "code": "scenario_math_error",
                        "path": "/underwriting",
                        "message": math_err,
                    }
                )

            # §12.9 & §12.2: hurdles.horizon_months consistency & scenario_version monotonicity
            uw_hurdles = uw.get("hurdles") if isinstance(uw.get("hurdles"), dict) else {}
            if (
                isinstance(uw_hurdles.get("horizon_months"), int)
                and isinstance(hv.get("horizon_months"), int)
                and int(uw_hurdles["horizon_months"]) != int(hv["horizon_months"])
            ):
                errors.append(
                    {
                        "code": "horizon_months_mismatch",
                        "path": "/underwriting/hurdles/horizon_months",
                        "message": f"`underwriting.hurdles.horizon_months`（{uw_hurdles['horizon_months']}）必须与 `house_view.horizon_months`（{hv['horizon_months']}）一致",
                    }
                )

            old_uw = (
                old_doc.get("underwriting")
                if isinstance(old_doc, dict) and isinstance(old_doc.get("underwriting"), dict)
                else None
            )
            old_hv = (
                old_doc.get("house_view")
                if isinstance(old_doc, dict) and isinstance(old_doc.get("house_view"), dict)
                else {}
            )
            if old_uw is not None and isinstance(old_uw.get("scenario_version"), int):
                old_ver = int(old_uw["scenario_version"])
                new_ver = int(uw.get("scenario_version") or 0)
                if new_ver < old_ver:
                    errors.append(
                        {
                            "code": "scenario_version_regression",
                            "path": "/underwriting/scenario_version",
                            "message": f"`underwriting.scenario_version` 不能小于上一版本（旧值 {old_ver}，当前 {new_ver}），请设为至少 {old_ver}",
                        }
                    )
                else:
                    uw_core_changed = (
                        old_uw.get("states") != uw.get("states")
                        or old_uw.get("anchor") != uw.get("anchor")
                        or old_uw.get("weight_bounds") != uw.get("weight_bounds")
                        or old_hv.get("horizon_months") != hv.get("horizon_months")
                    )
                    if uw_core_changed and new_ver < old_ver + 1:
                        errors.append(
                            {
                                "code": "scenario_version_not_incremented",
                                "path": "/underwriting/scenario_version",
                                "message": (
                                    f"修改三情景 `underwriting.states`、基准股价 `underwriting.anchor`、情景权重区间 `underwriting.weight_bounds` "
                                    f"或投资期限 `house_view.horizon_months` 时，`underwriting.scenario_version` 必须递增（旧值 {old_ver}，当前 {new_ver}，请设为 {old_ver + 1}）"
                                ),
                            }
                        )

            valid_ev_ids = {
                str(e.get("id"))
                for e in (new_doc.get("evidence") or [])
                if isinstance(e, dict) and e.get("id")
            }

            has_debate_doc = isinstance(debate_doc, dict)
            known_debate_ids: set[str] = set()
            if has_debate_doc:
                for d in debate_doc.get("debates") or []:
                    if isinstance(d, dict) and d.get("id"):
                        known_debate_ids.add(str(d["id"]))

            # Check each state has condition/summary, links D* or K*, and eps_pe / reference requirements (§3.3)
            for st_idx, st in enumerate(uw.get("states") or []):
                if not isinstance(st, dict):
                    continue
                if not str(st.get("condition") or st.get("summary") or "").strip():
                    errors.append(
                        {
                            "code": "state_missing_condition",
                            "path": f"/underwriting/states/{st_idx}/condition",
                            "message": f"`underwriting.states` 中 `{st.get('name')}` 情景必须填写非空的成立条件 `condition`（或 `summary`）",
                        }
                    )
                d_ids = [str(x).strip() for x in (st.get("debate_ids") or []) if str(x).strip()]
                k_ids = [str(x).strip() for x in (st.get("driver_ids") or []) if str(x).strip()]
                if not d_ids and not k_ids:
                    errors.append(
                        {
                            "code": "state_missing_driver_or_debate",
                            "path": f"/underwriting/states/{st_idx}",
                            "message": f"`underwriting.states` 中 `{st.get('name')}` 情景必须至少填写一个 `debate_ids`（`D*`）或 `driver_ids`（`K*`）",
                        }
                    )
                if has_debate_doc:
                    bad_state_d_ids = [d_id for d_id in d_ids if d_id not in known_debate_ids]
                    if bad_state_d_ids:
                        errors.append(
                            {
                                "code": "state_unknown_debate_ref",
                                "path": f"/underwriting/states/{st_idx}/debate_ids",
                                "message": f"`underwriting.states` 中 `{st.get('name')}` 情景的 `debate_ids` 引用了 `debate` 文档中不存在的分歧 ID {bad_state_d_ids}；请先在 `debate` 文档中添加或改为已有的 `D*`",
                            }
                        )
                st_calc = st.get("calc") if isinstance(st.get("calc"), dict) else {}
                st_method = st_calc.get("method")
                if st_method == "eps_pe":
                    st_basis = str(st_calc.get("basis") or "").strip()
                    st_period = str(st_calc.get("period") or "").strip()
                    st_sources = st_calc.get("sources") if isinstance(st_calc.get("sources"), list) else []
                    if not st_basis or not st_period or not st_sources:
                        errors.append(
                            {
                                "code": "eps_pe_missing_metadata",
                                "path": f"/underwriting/states/{st_idx}/calc",
                                "message": f"`underwriting.states` 中 `{st.get('name')}` 情景的 `calc.method` 为 `eps_pe` 时，必须同时填写非空的 `basis`、`period` 与 `sources`",
                            }
                        )
                    else:
                        for s_item in st_sources:
                            s_ref_id = (
                                str(s_item.get("ref") or s_item.get("id") or "").strip()
                                if isinstance(s_item, dict)
                                else str(s_item or "").strip()
                            )
                            if s_ref_id.startswith("E-") and s_ref_id not in valid_ev_ids:
                                errors.append(
                                    {
                                        "code": "eps_pe_invalid_source_ref",
                                        "path": f"/underwriting/states/{st_idx}/calc/sources",
                                        "message": f"`underwriting.states` 中 `{st.get('name')}` 情景的 `calc.sources` 引用了 `evidence` 中不存在的证据 ID `{s_ref_id}`；请先在 `evidence` 中添加该条证据",
                                    }
                                )
                elif st_method == "reference" and not _is_resolvable_calc_ref(
                    st_calc.get("ref"), new_doc, models_doc=models_doc
                ):
                    errors.append(
                        {
                            "code": "unresolvable_reference_ref",
                            "path": f"/underwriting/states/{st_idx}/calc/ref",
                            "message": (
                                f"`underwriting.states` 中 `{st.get('name')}` 情景的 `calc.method` 为 `reference` 时，`calc.ref` 必须指向："
                                "（1）`models` 文档中已登记的模型单元格（如 `model/dcf.xlsx#Summary!G42`）；"
                                "或（2）`evidence` 中 `source.type` 为 `model`（指向已登记模型单元格）或 `internal_note`（`fact` 含完整算式 `=` 及输入输出数值）的证据 ID"
                            ),
                        }
                    )

            stress_obj = uw.get("stress")
            if isinstance(stress_obj, dict) and isinstance(stress_obj.get("calc"), dict):
                str_calc = stress_obj["calc"]
                str_method = str_calc.get("method")
                if str_method == "eps_pe":
                    str_basis = str(str_calc.get("basis") or "").strip()
                    str_period = str(str_calc.get("period") or "").strip()
                    str_sources = str_calc.get("sources") if isinstance(str_calc.get("sources"), list) else []
                    if not str_basis or not str_period or not str_sources:
                        errors.append(
                            {
                                "code": "eps_pe_missing_metadata",
                                "path": "/underwriting/stress/calc",
                                "message": "`underwriting.stress.calc.method` 为 `eps_pe` 时，必须同时填写非空的 `basis`、`period` 与 `sources`",
                            }
                        )
                elif str_method == "reference" and not _is_resolvable_calc_ref(
                    str_calc.get("ref"), new_doc, models_doc=models_doc
                ):
                    errors.append(
                        {
                            "code": "unresolvable_reference_ref",
                            "path": "/underwriting/stress/calc/ref",
                            "message": (
                                "`underwriting.stress.calc.method` 为 `reference` 时，`calc.ref` 必须指向："
                                "（1）`models` 文档中已登记的模型单元格（如 `model/dcf.xlsx#Summary!G42`）；"
                                "或（2）`evidence` 中 `source.type` 为 `model`（指向已登记模型单元格）或 `internal_note`（`fact` 含完整算式 `=` 及输入输出数值）的证据 ID"
                            ),
                        }
                    )

            # §12.4 & §12.5: act_now stress, borrow_cost_annual, protection & needs_financing rules
            if actionability == "act_now" and not isinstance(uw.get("stress"), dict):
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/underwriting/stress",
                        "message": "`house_view.actionability` 为 `act_now` 时，必须填写 `underwriting.stress` 压力情景（含 `calc`、`condition`、`needs_financing`）",
                    }
                )
            if stance == "short" and uw.get("borrow_cost_annual") is None:
                if actionability == "act_now":
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/underwriting/borrow_cost_annual",
                            "message": "`house_view.stance` 为 `short` 且 `underwriting.borrow_cost_annual` 为 `null` 时，`house_view.actionability` 不得为 `act_now`，请改为 `wait_evidence`（或 `pass`）",
                        }
                    )
                elif actionability not in ("wait_evidence", "pass", None):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/actionability",
                            "message": "`house_view.stance` 为 `short` 且 `underwriting.borrow_cost_annual` 为 `null` 时，`house_view.actionability` 只能设为 `wait_evidence`（或 `pass`）",
                        }
                    )

            prot_info = _extract_protection_info(uw.get("protection"))
            if not prot_info["complete"]:
                errors.append(
                    {
                        "code": "protection_incomplete",
                        "path": "/underwriting/protection",
                        "message": f"`underwriting.protection` 缺少必填字段：{prot_info['missing']}（须完整填写 `survival`、`needs_financing`、`value`、`value_range`、`path`、`failure`）",
                    }
                )
            nf_val = prot_info["needs_financing"]
            surv_text = prot_info.get("surv_text") or ""
            stress_nf = (
                uw["stress"].get("needs_financing")
                if isinstance(uw.get("stress"), dict)
                else None
            )
            if nf_val == "unknown" or stress_nf == "unknown":
                if "生存保护充分" in surv_text or "无需融资" in surv_text:
                    errors.append(
                        {
                            "code": "protection_unknown_financing_claim",
                            "path": "/underwriting/protection/survival",
                            "message": "`needs_financing` 为 `unknown` 时，`underwriting.protection.survival` 不得声称“生存保护充分”或“无需融资”；请如实说明融资不确定性，或核实后将 `needs_financing` 改为 `no`",
                        }
                    )
                if stance == "long" and actionability == "act_now":
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/actionability",
                            "message": "`house_view.stance` 为 `long` 且 `needs_financing` 为 `unknown` 时，`house_view.actionability` 不得为 `act_now`；请核实后将 `needs_financing` 设为 `yes` 或 `no`，或将 `actionability` 改为 `wait_evidence`",
                        }
                    )

            # Breakeven verdict directional & vertex feasibility checks (§12.1–§12.4)
            bv_list = uw.get("breakeven_verdict") if isinstance(uw.get("breakeven_verdict"), list) else []
            bv_by_side: dict[str, dict[str, Any]] = {}
            wb_eval = eval_res.get("weight_bounds_eval")
            v_tr_monotonic = bool((eval_res.get("order_check") or {}).get("v_tr_monotonic", True))

            import re

            for bv_idx, bv_item in enumerate(bv_list):
                if isinstance(bv_item, dict) and bv_item.get("side") in ("long", "short"):
                    side_k = str(bv_item["side"])
                    bv_by_side[side_k] = bv_item
                    v_word = bv_item.get("verdict")
                    b_row_val = bv_item.get("b_row")
                    if b_row_val not in (0.25, 0.5):
                        errors.append(
                            {
                                "code": "invalid_breakeven_b_row",
                                "path": f"/underwriting/breakeven_verdict/{bv_idx}/b_row",
                                "message": f"`breakeven_verdict[{bv_idx}].b_row` 必须为 `0.25` 或 `0.5`（当前为 `{b_row_val}`）",
                            }
                        )
                    d_refs = [str(x).strip() for x in (bv_item.get("debate_ids") or []) if str(x).strip()]
                    bad_d_refs = [
                        dr
                        for dr in d_refs
                        if not re.match(r"^D\d+$", dr)
                        or (has_debate_doc and dr not in known_debate_ids)
                    ]
                    if not d_refs or bad_d_refs:
                        errors.append(
                            {
                                "code": "breakeven_missing_debate_ref",
                                "path": f"/underwriting/breakeven_verdict/{bv_idx}/debate_ids",
                                "message": f"`breakeven_verdict[{bv_idx}].debate_ids` 必须引用 `debate` 文档中已定义的分歧 ID `D*`（当前为 {d_refs}）",
                            }
                        )
                    if side_k == "long" and not v_tr_monotonic and v_word != "cannot_judge":
                        errors.append(
                            {
                                "code": "consistency_matrix_violation",
                                "path": f"/underwriting/breakeven_verdict/{bv_idx}/verdict",
                                "message": "三情景含现金回报终值中点未按 `bear` < `base` < `bull` 严格递增时，`side` 为 `long` 的 `breakeven_verdict.verdict` 必须设为 `cannot_judge`",
                            }
                        )
                    if isinstance(wb_eval, dict) and wb_eval.get("feasible"):
                        side_eval = wb_eval.get(side_k) or {}
                        if v_word in ("clearly_above", "around") and not side_eval.get("any_pass"):
                            errors.append(
                                {
                                    "code": "consistency_matrix_violation",
                                    "path": f"/underwriting/breakeven_verdict/{bv_idx}/verdict",
                                    "message": f"`breakeven_verdict` 中 `side` 为 `{side_k}` 的 `verdict` 设为 `{v_word}`，但 `underwriting.weight_bounds` 允许的任何一种权重组合都达不到回报门槛；请将 `verdict` 改为 `below`（并同步调整 `house_view.stance`），或复核 `weight_bounds` 与 `states`",
                                }
                            )

            if stance in ("long", "short") and stance not in bv_by_side:
                errors.append(
                    {
                        "code": "consistency_matrix_violation",
                        "path": "/underwriting/breakeven_verdict",
                        "message": f"`house_view.stance` 为 `{stance}` 时，`underwriting.breakeven_verdict` 必须包含 `side` 为 `{stance}` 的判定",
                    }
                )
            elif stance == "neutral":
                if "long" not in bv_by_side or "short" not in bv_by_side:
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/underwriting/breakeven_verdict",
                            "message": "`house_view.stance` 为 `neutral` 时，`underwriting.breakeven_verdict` 必须同时包含 `side` 为 `long` 与 `side` 为 `short` 两条判定",
                        }
                    )
                else:
                    if (
                        bv_by_side["long"].get("verdict") == "clearly_above"
                        or bv_by_side["short"].get("verdict") == "clearly_above"
                    ) and not (bc in ("implementation", "timing") and bool(bc_text)):
                        errors.append(
                            {
                                "code": "consistency_matrix_violation",
                                "path": "/underwriting/breakeven_verdict",
                                "message": "`house_view.stance` 为 `neutral` 时，`long` 与 `short` 两个方向的 `breakeven_verdict.verdict` 均不得为 `clearly_above`，除非 `house_view.binding_constraint` 为 `implementation` 或 `timing` 且填写了非空的 `binding_constraint_text`",
                            }
                        )

            # Active side checks for long / short (§12.4)
            if stance in ("long", "short") and stance in bv_by_side:
                active_verdict = bv_by_side[stance].get("verdict")
                if active_verdict not in ("clearly_above", "around"):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/underwriting/breakeven_verdict",
                            "message": f"`house_view.stance` 为 `{stance}` 时，`side` 为 `{stance}` 的 `breakeven_verdict.verdict` 必须为 `clearly_above` 或 `around`（当前为 `{active_verdict}`）；若赔率不达标请将 `stance` 改为 `neutral`",
                        }
                    )
                if actionability == "act_now" and active_verdict != "clearly_above":
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/actionability",
                            "message": f"`house_view.actionability` 为 `act_now` 时，`side` 为 `{stance}` 的 `breakeven_verdict.verdict` 必须为 `clearly_above`（当前为 `{active_verdict}`）；请将 `actionability` 改为 `wait_price` 或 `wait_evidence`",
                        }
                    )
                if active_verdict == "around" and conviction == "high":
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/conviction",
                            "message": "当前方向的 `breakeven_verdict.verdict` 为 `around` 时，`house_view.conviction` 最高只能设为 `medium`，不得为 `high`",
                        }
                    )

            # Conviction caps: §2.2 multiple_share > 50% cap & §12.3 two paths for high conviction
            if stance in ("long", "short") and conviction == "high":
                ms_info = eval_res.get("multiple_share") if isinstance(eval_res.get("multiple_share"), dict) else {}
                ms_val = (
                    ms_info.get("multiple_share_bull")
                    if stance == "long"
                    else ms_info.get("multiple_share_bear")
                )
                rerating_ok = bool(
                    uw.get("rerating_observed") is True
                    and isinstance(uw.get("rerating_sources"), list)
                    and len(uw["rerating_sources"]) > 0
                )
                if ms_val is not None and float(ms_val) > 0.5 + 1e-9 and not rerating_ok:
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/conviction",
                            "message": (
                                f"估值倍数变化对目标回报的贡献占比为 {float(ms_val):.1%}（超过 50%），且未提供已兑现的估值重估证据"
                                "（将 `underwriting.rerating_observed` 设为 `true` 并填写非空 `underwriting.rerating_sources`）时，"
                                "`house_view.conviction` 最高只能设为 `medium`"
                            ),
                        }
                    )

                wb_raw = uw.get("weight_bounds") if isinstance(uw.get("weight_bounds"), dict) else None
                wb_rationale = str((wb_raw or {}).get("rationale") or "").strip().lower()
                wb_sources = (wb_raw or {}).get("sources") if isinstance(wb_raw, dict) else None
                is_risk_neutral = "风险中性" in wb_rationale or "risk-neutral" in wb_rationale or "risk neutral" in wb_rationale
                path1_ok = bool(
                    wb_raw is not None
                    and isinstance(wb_sources, list)
                    and len(wb_sources) > 0
                    and not is_risk_neutral
                    and isinstance(wb_eval, dict)
                    and wb_eval.get("feasible")
                    and bool((wb_eval.get(stance) or {}).get("all_pass"))
                )
                adv_ok = bool((eval_res.get("adverse_move") or {}).get("ok_for_act_now"))
                path2_ok = bool(
                    adv_ok
                    and (
                        eval_res.get("no_bull_needed_all_lo")
                        if stance == "long"
                        else eval_res.get("no_bear_needed_all_hi")
                    )
                )
                if not (path1_ok or path2_ok):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/conviction",
                            "message": (
                                "`house_view.conviction` 设为 `high` 须满足以下两项条件之一："
                                "（1）已填写 `underwriting.weight_bounds`（含非空 `sources`，且 `rationale` 非风险中性假设），且在权重区间允许的所有组合下（含最不利的组合）都跨过回报门槛；"
                                "（2）在 `base` 权重为 25% 与 50% 的全保守端点下，无需 `bull`（做多）或 `bear`（做空）权重即可达到回报门槛，且不利端点变动不超过上限。"
                                "若均不满足，请将 `conviction` 改为 `medium` 或 `low`"
                            ),
                        }
                    )

            # Row 1: act_now adverse move check (endpoints per §12.4)
            if actionability == "act_now" and stance in ("long", "short"):
                adv = eval_res.get("adverse_move")
                if isinstance(adv, dict) and not adv.get("ok_for_act_now"):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/actionability",
                            "message": (
                                f"`house_view.actionability` 为 `act_now` 时，不利情景端点的逆向波动不得超过 {adv.get('max_adverse_move_act_now', 0.25):.0%} "
                                f"（做多看 `bear` 下限含现金回报跌幅，做空看 `bull` 价格上限涨幅；当前为 {adv.get('adverse_move', 0.0):.2%}）；"
                                "请将 `actionability` 改为 `wait_price` 或 `wait_evidence`"
                            ),
                        }
                    )

            # Row 2: wait_price entry_band check (exempt only when entry_band, states, AND anchor are unchanged)
            if actionability == "wait_price":
                old_hv_eb = (
                    (old_doc or {}).get("house_view")
                    if isinstance((old_doc or {}).get("house_view"), dict)
                    else {}
                )
                old_uw_eb = (
                    (old_doc or {}).get("underwriting")
                    if isinstance((old_doc or {}).get("underwriting"), dict)
                    else {}
                )
                drift_exempt = bool(
                    old_doc is not None
                    and old_hv_eb.get("actionability") == "wait_price"
                    and old_hv_eb.get("entry_band") is not None
                    and old_hv_eb.get("entry_band") == hv.get("entry_band")
                    and old_uw_eb.get("states") == uw.get("states")
                    and old_uw_eb.get("anchor") == uw.get("anchor")
                )
                eb_check = eval_res.get("entry_band_check")
                if not drift_exempt and isinstance(eb_check, dict) and not eb_check.get("ok"):
                    errors.append(
                        {
                            "code": "consistency_matrix_violation",
                            "path": "/house_view/entry_band",
                            "message": "; ".join(eb_check.get("errors") or ["invalid entry_band"]),
                        }
                    )

            # §12.7 Drift rule: broken pillars must be closed before claiming act_now
            broken_pillars = [
                str(p.get("id"))
                for p in (new_doc.get("pillars") or [])
                if isinstance(p, dict) and p.get("status") == "broken"
            ]
            if broken_pillars and stance in ("long", "short") and actionability == "act_now":
                errors.append(
                    {
                        "code": "drift_broken_pillar_unclosed",
                        "path": "/pillars",
                        "message": (
                            f"支柱 {broken_pillars} 的 `status` 为 `broken` 时，`house_view.actionability` 不得标为 `act_now`；"
                            "请先将已证伪支柱的 `status` 更新为 `changed` 或 `retired`，并重算三情景 `underwriting.states`、递增 `scenario_version`"
                        ),
                    }
                )

    elif doc_key == "debate":
        doc_as_of_cmp = _parse_cmp_timestamp(new_doc.get("as_of"))
        old_debates_list = [
            d for d in ((old_doc or {}).get("debates") or []) if isinstance(d, dict)
        ]
        new_debates_list = [
            d for d in (new_doc.get("debates") or []) if isinstance(d, dict)
        ]
        old_debates_map = {
            str(d.get("id")): d for d in old_debates_list if d.get("id")
        }
        new_debates_map = {
            str(d.get("id")): d for d in new_debates_list if d.get("id")
        }

        # Collect canonical t0 per event_id across old_doc and new_doc (§12.6)
        canonical_event_t0: dict[str, str] = {}
        for d in old_debates_list:
            for exp in d.get("expectations") or []:
                if isinstance(exp, dict):
                    ev_id_s = str(exp.get("event_id") or "").strip()
                    t0_s = _parse_cmp_timestamp(exp.get("t0"))
                    if ev_id_s and t0_s and ev_id_s not in canonical_event_t0:
                        canonical_event_t0[ev_id_s] = t0_s
        for d in new_debates_list:
            for exp in d.get("expectations") or []:
                if isinstance(exp, dict):
                    ev_id_s = str(exp.get("event_id") or "").strip()
                    t0_s = _parse_cmp_timestamp(exp.get("t0"))
                    if ev_id_s and t0_s and ev_id_s not in canonical_event_t0:
                        canonical_event_t0[ev_id_s] = t0_s

        # Verify no debate containing frozen_pre_event=true was deleted or had its frozen item removed
        for old_d in old_debates_list:
            old_d_id = str(old_d.get("id") or "")
            old_frozen_in_d = [
                e
                for e in (old_d.get("expectations") or [])
                if isinstance(e, dict) and e.get("frozen_pre_event") is True
            ]
            if not old_frozen_in_d:
                continue
            new_d = new_debates_map.get(old_d_id)
            if new_d is None:
                for f_exp in old_frozen_in_d:
                    errors.append(
                        {
                            "code": "frozen_expectation_immutable",
                            "path": "/debates",
                            "message": (
                                f"分歧 `{old_d_id}` 含有事件前已锁定的预期（`metric` 为 `{f_exp.get('metric')}`，"
                                f"`event_id` 为 `{f_exp.get('event_id')}`），不得删除整个分歧条目；"
                                "若分歧已结束，请将其 `status` 改为 `resolved` 或 `dormant`"
                            ),
                        }
                    )

        for idx, d in enumerate(new_debates_list):
            if d.get("status") == "open":
                missing_v2: list[str] = []
                if d.get("rank") is None:
                    missing_v2.append("rank")
                if not str(d.get("why_it_matters") or "").strip():
                    missing_v2.append("why_it_matters")
                ml = d.get("market_lean")
                if not isinstance(ml, dict) or not ml.get("side"):
                    missing_v2.append("market_lean")
                if not d.get("transmission"):
                    missing_v2.append("transmission")
                if missing_v2:
                    errors.append(
                        {
                            "code": "active_debate_missing_v2_fields",
                            "path": f"/debates/{idx}",
                            "message": f"`status` 为 `open` 的分歧 `{d.get('id', idx)}` 必须填写非空的 {missing_v2}（其中 `market_lean` 须包含 `side`）",
                        }
                    )

            old_d = old_debates_map.get(str(d.get("id")))
            old_exps_list = [
                e for e in ((old_d or {}).get("expectations") or []) if isinstance(e, dict)
            ]
            old_frozen_list = [e for e in old_exps_list if e.get("frozen_pre_event") is True]
            new_exps_list = [e for e in (d.get("expectations") or []) if isinstance(e, dict)]

            # Verify every previously frozen expectation is preserved verbatim (cannot be deleted or mutated in any field)
            for f_exp in old_frozen_list:
                if f_exp not in new_exps_list:
                    errors.append(
                        {
                            "code": "frozen_expectation_immutable",
                            "path": f"/debates/{idx}/expectations",
                            "message": (
                                f"事件前已锁定的预期（`metric` 为 `{f_exp.get('metric')}`，`period` 为 `{f_exp.get('period')}`，"
                                f"`event_id` 为 `{f_exp.get('event_id')}`）不得删除、不得将 `frozen_pre_event` 改为 `false`，也不得修改其中任何字段"
                            ),
                        }
                    )

            for e_idx, exp in enumerate(d.get("expectations") or []):
                if not isinstance(exp, dict):
                    continue
                for est_key in ("street", "buyside", "house", "consensus", "market_implied"):
                    est = exp.get(est_key)
                    if isinstance(est, dict) and not (est.get("sources") or []):
                        errors.append(
                            {
                                "code": "expectation_missing_sources",
                                "path": f"/debates/{idx}/expectations/{e_idx}/{est_key}/sources",
                                "message": f"`expectations[{e_idx}].{est_key}.sources` 必须至少包含一条来源引用",
                            }
                        )
                # Basis consistency check across street / buyside / house (§3.4 & Lead Decision 1):
                # Inconsistent or unknown basis is allowed when gap is not computed (emits soft warning),
                # but hard-rejected if gap is computed.
                active_ests = [
                    exp[k]
                    for k in ("street", "buyside", "house")
                    if isinstance(exp.get(k), dict) and exp[k].get("value") is not None
                ]
                bases = {str(e.get("basis") or "unknown") for e in active_ests}
                if exp.get("gap") is not None and ("unknown" in bases or len(bases) > 1):
                    errors.append(
                        {
                            "code": "expectation_basis_mismatch",
                            "path": f"/debates/{idx}/expectations/{e_idx}/gap",
                            "message": (
                                f"同一 `expectations` 条目下 `street`、`buyside`、`house` 的口径 `basis`（{sorted(bases)}）不一致或为 `unknown` 时，"
                                "不得填写预期差 `gap`；请将 `gap` 设为 `null`，或先将各方统一为同一确定口径"
                            ),
                        }
                    )

                # §12.6 frozen_pre_event verification
                if exp.get("frozen_pre_event") is True:
                    if exp.get("reconstructed") is True:
                        errors.append(
                            {
                                "code": "frozen_expectation_post_event",
                                "path": f"/debates/{idx}/expectations/{e_idx}/frozen_pre_event",
                                "message": "`reconstructed` 为 `true`（事后回填）的预期不得将 `frozen_pre_event` 设为 `true`；请将 `frozen_pre_event` 设为 `false`",
                            }
                        )
                    ev_id_str = str(exp.get("event_id") or "").strip()
                    t0_cmp = _parse_cmp_timestamp(exp.get("t0"))
                    pub_cmp = _parse_cmp_timestamp(exp.get("published_at"))
                    acq_cmp = _parse_cmp_timestamp(exp.get("acquired_at"))
                    frz_cmp = _parse_cmp_timestamp(exp.get("frozen_at"))
                    canon_t0 = canonical_event_t0.get(ev_id_str, t0_cmp)
                    if not (ev_id_str and t0_cmp and pub_cmp and acq_cmp and frz_cmp):
                        errors.append(
                            {
                                "code": "frozen_expectation_invalid",
                                "path": f"/debates/{idx}/expectations/{e_idx}",
                                "message": "`frozen_pre_event` 为 `true` 时，必须填写非空的 `event_id` 及合法的 ISO 时间戳 `t0`、`published_at`、`acquired_at`、`frozen_at`",
                            }
                        )
                    elif canon_t0 is not None and t0_cmp != canon_t0:
                        errors.append(
                            {
                                "code": "frozen_expectation_post_event",
                                "path": f"/debates/{idx}/expectations/{e_idx}/t0",
                                "message": f"同一事件 `{ev_id_str}` 的发布时间 `t0` 不可修改（已登记 `t0` 为 `{canon_t0}`，当前为 `{t0_cmp}`）；请恢复为 `{canon_t0}`",
                            }
                        )
                    elif not (
                        pub_cmp < (canon_t0 or t0_cmp)
                        and acq_cmp < (canon_t0 or t0_cmp)
                        and frz_cmp < (canon_t0 or t0_cmp)
                    ):
                        errors.append(
                            {
                                "code": "frozen_expectation_post_event",
                                "path": f"/debates/{idx}/expectations/{e_idx}/frozen_pre_event",
                                "message": (
                                    "`frozen_pre_event` 为 `true` 要求 `published_at`、`acquired_at`、`frozen_at` 均严格早于事件发布时间 `t0`；"
                                    "事后采集的预期请设为 `frozen_pre_event: false, reconstructed: true`"
                                ),
                            }
                        )
                    elif (
                        exp not in old_frozen_list
                        and doc_as_of_cmp is not None
                        and doc_as_of_cmp >= (canon_t0 or t0_cmp)
                    ):
                        errors.append(
                            {
                                "code": "frozen_expectation_post_event",
                                "path": f"/debates/{idx}/expectations/{e_idx}/frozen_pre_event",
                                "message": "文档 `as_of` 已到达或晚于事件发布时间 `t0`，不得再为该事件新增或补标 `frozen_pre_event` 为 `true` 的预期；事后补充请设为 `frozen_pre_event: false, reconstructed: true`",
                            }
                        )

    elif doc_key == "drivers":
        def _has_point_val(val: Any) -> bool:
            if val is None or isinstance(val, bool):
                return False
            if isinstance(val, (int, float)):
                return True
            return bool(str(val).strip())

        for idx, drv in enumerate(new_doc.get("drivers") or []):
            if not isinstance(drv, dict):
                continue
            resp = drv.get("response")
            if isinstance(resp, dict):
                shape = resp.get("shape")
                if shape not in ("linear", "unknown", None):
                    has_threshold_text = bool(str(resp.get("threshold_text") or "").strip())
                    pts = resp.get("points")
                    valid_pts = [
                        pt
                        for pt in (pts if isinstance(pts, list) else [])
                        if isinstance(pt, dict)
                        and (_has_point_val(pt.get("driver_move")) or _has_point_val(pt.get("x")))
                        and any(
                            _has_point_val(pt.get(out_k))
                            for out_k in ("eps_impact", "fcf_impact", "multiple_impact", "y")
                        )
                    ]
                    has_three_points = len(valid_pts) >= 3
                    if not (has_threshold_text or has_three_points):
                        errors.append(
                            {
                                "code": "driver_response_missing_threshold_text",
                                "path": f"/drivers/{idx}/response",
                                "message": (
                                    f"`response.shape` 为 `{shape}`（非 `linear` / `unknown`）时，必须填写非空的 `threshold_text`，"
                                    "或在 `response.points` 中提供至少 3 个同时含输入变动（`driver_move` 或 `x`）和产出影响（`eps_impact`、`multiple_impact`、`fcf_impact` 或 `y`）的计算点"
                                ),
                            }
                        )

    elif doc_key == "agenda":
        watch = new_doc.get("watch")
        if isinstance(watch, dict):
            for idx, ev in enumerate(watch.get("events") or []):
                if not isinstance(ev, dict):
                    continue
                if ev.get("date_status") in ("estimated", "unknown") and not str(
                    ev.get("review_by") or ""
                ).strip():
                    errors.append(
                        {
                            "code": "watch_event_missing_review_by",
                            "path": f"/watch/events/{idx}/review_by",
                            "message": f"`watch.events[{idx}].date_status` 为 `{ev.get('date_status')}` 时，必须填写非空的复核日期 `review_by`（`YYYY-MM-DD`）",
                        }
                    )

    return errors


def validate_update(
    doc_key: str,
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any],
    *,
    source: str = "tool",
    models_doc: dict[str, Any] | None = None,
    agenda_doc: dict[str, Any] | None = None,
    debate_doc: dict[str, Any] | None = None,
    reason: str = "updated",
) -> ValidationResult:
    """Validate ``new_doc`` against PK.2 schema, hard invariants, v2 consistency matrix, and soft warning rules."""
    hard_errors = _check_hard_invariants(
        doc_key,
        old_doc,
        new_doc,
        source=source,
        models_doc=models_doc,
    )
    schema_errors = validate_schema(doc_key, new_doc)
    v2_errors = _check_v2_semantics(
        doc_key,
        old_doc,
        new_doc,
        models_doc=models_doc,
        agenda_doc=agenda_doc,
        debate_doc=debate_doc,
    )
    warnings = _check_soft_warnings(
        doc_key,
        old_doc,
        new_doc,
        reason=reason,
        models_doc=models_doc,
        debate_doc=debate_doc,
    )
    return ValidationResult(
        errors=[*hard_errors, *schema_errors, *v2_errors],
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
    # DALTON-HOOK(payoff-assessment): changed/reason 的组合在 schema 之外单独校验，
    # 报错信息是模型可直接照改的一句话；jsonschema 的 if/then 报错做不到这一点。
    payoff = verdict.get("payoff_assessment")
    errors = list(schema_errors)
    if isinstance(payoff, dict) and "changed" in payoff and "reason" in payoff:
        changed = payoff.get("changed")
        reason = payoff.get("reason")
        if changed is False and reason != "none":
            errors.append(
                {
                    "code": "payoff_reason_mismatch",
                    "path": "/payoff_assessment/reason",
                    "message": (
                        f"payoff_assessment.changed=false 时 reason 必须是 none（当前 {reason!r}）；"
                        "赔率没变就写 {\"changed\": false, \"reason\": \"none\"}"
                    ),
                }
            )
        elif changed is True and reason == "none":
            errors.append(
                {
                    "code": "payoff_reason_mismatch",
                    "path": "/payoff_assessment/reason",
                    "message": (
                        "payoff_assessment.changed=true 时 reason 不得是 none；"
                        "从 price_crossed_band / scenario_revised / expectations_migrated / "
                        "catalyst_expired / protection_failed 中选一个"
                    ),
                }
            )
    return ValidationResult(errors=errors, warnings=warnings)
