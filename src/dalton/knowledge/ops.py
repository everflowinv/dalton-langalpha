"""ID-addressed operations language and server-maintained fields for knowledge docs (PK.4)."""

from __future__ import annotations

import copy
import difflib
import hashlib
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from src.dalton.settings import get_dalton_config


class OpError(ValueError):
    """Raised when an operation path or payload cannot be applied."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{path}: {code} ({message})")
        self.code = code
        self.path = path
        self.message = message


_APPEND_ONLY_ARRAY_NAMES = frozenset(
    {
        "evidence",
        "house_view_history",
        "status_history",
        "history",
        "guidance_history",
        "credibility_notes",
        "attempts",
        "versions",
    }
)

_AS_OF_DOCS = frozenset({"thesis", "debate", "drivers", "management", "agenda"})

_DOC_PATH_ALIASES: dict[str, dict[str, str]] = {
    "agenda": {
        "questions": "open_questions",
        "open_question": "open_questions",
        "validations": "next_validations",
        "validation_queue": "next_validations",
        "catalysts": "watch/events",
        "events": "watch/events",
        "next_actions": "wake_conditions",
        "handoffs": "pm_handoffs",
    },
    "thesis": {
        "evidence_log": "evidence",
        "evidences": "evidence",
        "thesis_pillars": "pillars",
        "summary": "one_liner",
    },
    "debate": {
        "debate_list": "debates",
        "items": "debates",
    },
    "drivers": {
        "key_drivers": "drivers",
        "driver_list": "drivers",
    },
    "lessons": {
        "lesson_list": "lessons",
        "items": "lessons",
    },
    "management": {
        "guidance": "guidance_history",
        "notes": "credibility_notes",
    },
}

_COLLECTION_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "pillars": {
        "statement": "claim",
        "title": "claim",
        "description": "claim",
        "summary": "claim",
    },
    "evidence": {
        "statement": "fact",
        "claim": "fact",
        "summary": "fact",
        "text": "fact",
    },
    "debates": {
        "title": "variable",
        "topic": "variable",
        "claim": "question",
        "statement": "question",
        "importance": "why_it_matters",
        "why": "why_it_matters",
    },
    "bull": {
        "statement": "case",
        "claim": "case",
        "text": "case",
        "summary": "case",
    },
    "bear": {
        "statement": "case",
        "claim": "case",
        "text": "case",
        "summary": "case",
    },
    "open_questions": {
        "statement": "question",
        "claim": "question",
        "text": "question",
        "title": "question",
        "question_text": "question",
        "reason": "why",
        "why_it_matters": "why",
    },
    "next_validations": {
        "due_date": "due",
        "date": "due",
        "description": "what",
        "title": "what",
        "statement": "what",
        "claim": "what",
        "expectation": "expected",
    },
    "events": {
        "event_date": "date",
        "due": "date",
        "due_date": "date",
        "action": "plan",
        "description": "plan",
        "what": "plan",
    },
    "lessons": {
        "claim": "statement",
        "lesson": "statement",
        "text": "statement",
        "summary": "statement",
        "when": "applies_when",
    },
    "drivers": {
        "title": "name",
        "metric": "name",
        "statement": "description",
        "claim": "description",
        "summary": "description",
    },
}


def _now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _day_stamp(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d")


def _parse_int_suffix(items: list[dict[str, Any]], prefix: str) -> int:
    max_n = 0
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for item in items:
        if isinstance(item, dict):
            raw_id = str(item.get("id") or "")
            m = pattern.match(raw_id)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n


def next_item_id(
    collection_name: str,
    existing_items: list[dict[str, Any]],
    *,
    parent_id: str | None = None,
    now: datetime | None = None,
    root_doc: dict[str, Any] | None = None,
) -> str:
    """Assign the next canonical ID for an item in ``collection_name``."""
    if collection_name == "pillars":
        return f"P{_parse_int_suffix(existing_items, 'P') + 1}"
    if collection_name in ("confirm", "warn", "break"):
        all_thresholds: list[dict[str, Any]] = []
        if root_doc and isinstance(root_doc.get("pillars"), list):
            for p in root_doc["pillars"]:
                th = (p or {}).get("thresholds") or {}
                for k in ("confirm", "warn", "break"):
                    all_thresholds.extend(th.get(k) or [])
        all_thresholds.extend(existing_items)
        return f"T{_parse_int_suffix(all_thresholds, 'T') + 1}"
    if collection_name == "evidence":
        day = _day_stamp(now)
        prefix = f"E-{day}-"
        max_n = _parse_int_suffix(existing_items, prefix)
        return f"{prefix}{max_n + 1:02d}"
    if collection_name == "debates":
        return f"D{_parse_int_suffix(existing_items, 'D') + 1}"
    if collection_name == "drivers":
        return f"K{_parse_int_suffix(existing_items, 'K') + 1}"
    if collection_name == "leading_indicators":
        p_id = parent_id or "K1"
        prefix = f"{p_id}."
        return f"{prefix}{_parse_int_suffix(existing_items, prefix) + 1}"
    if collection_name == "guidance_history":
        return f"G-{_parse_int_suffix(existing_items, 'G-') + 1:03d}"
    if collection_name == "lessons":
        return f"L-{_parse_int_suffix(existing_items, 'L-') + 1:03d}"
    if collection_name == "open_questions":
        return f"Q-{_parse_int_suffix(existing_items, 'Q-') + 1:03d}"
    if collection_name == "next_validations":
        return f"V-{_parse_int_suffix(existing_items, 'V-') + 1:03d}"
    if collection_name == "events":
        return f"W{_parse_int_suffix(existing_items, 'W') + 1}"
    if collection_name == "wake_conditions":
        return f"C{_parse_int_suffix(existing_items, 'C') + 1}"
    if collection_name == "pm_handoffs":
        return f"H{_parse_int_suffix(existing_items, 'H') + 1}"
    if collection_name == "versions":
        return f"v{_parse_int_suffix(existing_items, 'v') + 1}"
    return f"ID-{len(existing_items) + 1}"


def _ensure_nested_ids_and_defaults(
    doc_key: str,
    doc: dict[str, Any],
    *,
    now: datetime | None = None,
    old_doc: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
) -> None:
    """Assign missing IDs and server timestamps across the document."""
    now_str = _now_iso(now)
    doc["schema_version"] = 1
    if doc_key in _AS_OF_DOCS:
        doc["as_of"] = now_str

    if doc_key == "thesis":
        doc.setdefault("house_view_history", [])
        doc.setdefault("evidence", [])
        if "official" not in doc:
            doc["official"] = None
        hv = doc.get("house_view")
        if isinstance(hv, dict):
            hv.setdefault("updated_at", now_str)
        pillars = doc.get("pillars")
        if isinstance(pillars, list):
            for idx, pillar in enumerate(pillars):
                if not isinstance(pillar, dict):
                    continue
                if not pillar.get("id"):
                    pillar["id"] = next_item_id(
                        "pillars", pillars[:idx], now=now, root_doc=doc
                    )
                pillar.setdefault("created_at", now_str)
                pillar.setdefault("provenance", "draft")
                th = pillar.get("thresholds")
                if isinstance(th, dict):
                    for bucket in ("confirm", "warn", "break"):
                        th.setdefault(bucket, [])
                        for t_item in th[bucket]:
                            if isinstance(t_item, dict) and not t_item.get("id"):
                                t_item["id"] = next_item_id(
                                    bucket, th[bucket], now=now, root_doc=doc
                                )
        evidence = doc.get("evidence")
        if isinstance(evidence, list):
            for idx, ev in enumerate(evidence):
                if not isinstance(ev, dict):
                    continue
                if not ev.get("id"):
                    ev["id"] = next_item_id(
                        "evidence", evidence[:idx], now=now, root_doc=doc
                    )
                ev.setdefault("at", now_str)

    elif doc_key == "debate":
        debates = doc.get("debates")
        if isinstance(debates, list):
            for idx, d in enumerate(debates):
                if not isinstance(d, dict):
                    continue
                if not d.get("id"):
                    d["id"] = next_item_id("debates", debates[:idx], now=now)
                d.setdefault("opened_at", now_str)
                d.setdefault("history", [])

    elif doc_key == "drivers":
        drivers = doc.get("drivers")
        if isinstance(drivers, list):
            for idx, drv in enumerate(drivers):
                if not isinstance(drv, dict):
                    continue
                if not drv.get("id"):
                    drv["id"] = next_item_id("drivers", drivers[:idx], now=now)
                drv.setdefault("updated_at", now_str)
                drv.setdefault("leading_indicators", [])
                inds = drv["leading_indicators"]
                if isinstance(inds, list):
                    for i_idx, ind in enumerate(inds):
                        if not isinstance(ind, dict):
                            continue
                        if not ind.get("id"):
                            ind["id"] = next_item_id(
                                "leading_indicators",
                                inds[:i_idx],
                                parent_id=drv["id"],
                                now=now,
                            )
                        readings = ind.get("readings")
                        if isinstance(readings, list) and len(readings) > 200:
                            ind["readings"] = readings[-200:]

    elif doc_key == "management":
        gh = doc.get("guidance_history")
        if isinstance(gh, list):
            for idx, g in enumerate(gh):
                if not isinstance(g, dict):
                    continue
                if not g.get("id"):
                    g["id"] = next_item_id("guidance_history", gh[:idx], now=now)
        _recompute_management_metrics(doc)

    elif doc_key == "lessons":
        lessons = doc.get("lessons")
        if isinstance(lessons, list):
            for idx, item in enumerate(lessons):
                if not isinstance(item, dict):
                    continue
                if not item.get("id"):
                    item["id"] = next_item_id("lessons", lessons[:idx], now=now)
                item.setdefault("created_at", now_str)
                item.setdefault("status", "active")

    elif doc_key == "agenda":
        doc.setdefault("open_questions", [])
        doc.setdefault("next_validations", [])
        doc.setdefault("watch", {})
        doc.setdefault("wake_conditions", [])
        old_qs = {
            str(oq.get("id")): oq
            for oq in ((old_doc or {}).get("open_questions") or [])
            if isinstance(oq, dict) and oq.get("id")
        }
        for idx, q in enumerate(doc["open_questions"]):
            if isinstance(q, dict):
                if not q.get("id"):
                    q["id"] = next_item_id(
                        "open_questions", doc["open_questions"][:idx], now=now
                    )
                q.setdefault("created_at", now_str)
                q.setdefault("attempts", [])
                _maintain_question_backoff(
                    q,
                    now=now,
                    old_question=old_qs.get(str(q.get("id"))),
                    evidence_ids=evidence_ids,
                )
        for idx, v in enumerate(doc["next_validations"]):
            if isinstance(v, dict) and not v.get("id"):
                v["id"] = next_item_id(
                    "next_validations", doc["next_validations"][:idx], now=now
                )
        watch = doc.get("watch")
        if isinstance(watch, dict) and isinstance(watch.get("events"), list):
            for idx, ev in enumerate(watch["events"]):
                if isinstance(ev, dict) and not ev.get("id"):
                    ev["id"] = next_item_id("events", watch["events"][:idx], now=now)
        for idx, wc in enumerate(doc["wake_conditions"]):
            if isinstance(wc, dict) and not wc.get("id"):
                wc["id"] = next_item_id(
                    "wake_conditions", doc["wake_conditions"][:idx], now=now
                )
        if isinstance(doc.get("pm_handoffs"), list):
            for idx, h in enumerate(doc["pm_handoffs"]):
                if isinstance(h, dict):
                    if not h.get("id"):
                        h["id"] = next_item_id(
                            "pm_handoffs", doc["pm_handoffs"][:idx], now=now
                        )
                    h.setdefault("since", now_str)

    elif doc_key == "models":
        versions = doc.get("versions")
        if isinstance(versions, list):
            for idx, v in enumerate(versions):
                if isinstance(v, dict):
                    if not v.get("id"):
                        v["id"] = next_item_id("versions", versions[:idx], now=now)
                    v.setdefault("created_at", now_str)
            if versions and not doc.get("current"):
                doc["current"] = versions[-1]["id"]


def _recompute_management_metrics(doc: dict[str, Any]) -> None:
    """Compute server-owned delta_vs_mid_pct, beat_rate, and median_surprise_pct."""
    gh = doc.get("guidance_history")
    if not isinstance(gh, list):
        return
    surprises: list[float] = []
    beats = 0
    evaluated = 0
    for item in gh:
        if not isinstance(item, dict):
            continue
        guide = item.get("guide") or {}
        actual = item.get("actual")
        if not isinstance(actual, dict) or actual.get("value") is None:
            continue
        act_val = float(actual["value"])
        mid: float | None = None
        if guide.get("point") is not None:
            mid = float(guide["point"])
        elif guide.get("low") is not None and guide.get("high") is not None:
            mid = (float(guide["low"]) + float(guide["high"])) / 2.0
        if mid is not None and mid != 0:
            delta_pct = round(((act_val - mid) / abs(mid)) * 100.0, 2)
            item["delta_vs_mid_pct"] = delta_pct
            surprises.append(delta_pct)
        outcome = item.get("outcome")
        if outcome and outcome not in ("pending", "withdrawn"):
            evaluated += 1
            if outcome in ("above_high", "in_range_upper", "met_point") or (
                mid is not None and act_val > mid
            ):
                beats += 1

    style = doc.get("guidance_style")
    if isinstance(style, dict):
        style["sample_n"] = evaluated
        if evaluated > 0:
            style["beat_rate"] = round(beats / evaluated, 4)
        if surprises:
            style["median_surprise_pct"] = round(float(statistics.median(surprises)), 2)


def _parse_iso_dt(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            return fallback
    return fallback


def _extract_question_evidence_ids(q: dict[str, Any] | None) -> set[str]:
    if not isinstance(q, dict):
        return set()
    ev_ids: set[str] = set()
    for item in q.get("evidence_ids") or []:
        if item:
            ev_ids.add(str(item))
    linked = q.get("linked")
    if isinstance(linked, dict):
        for item in linked.get("evidence_ids") or []:
            if item:
                ev_ids.add(str(item))
    return ev_ids


def _maintain_question_backoff(
    question: dict[str, Any],
    *,
    now: datetime | None = None,
    old_question: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
) -> None:
    """Compute evidence_fp, stalled_since, and next_attempt_after on agenda questions.

    Backoff formula (P0G §0.2 item 2 / R5):
      backoff = clamp(0.5 * stalled_duration, 24h, 30d)
    Clears backoff immediately when new evidence is linked to the question.
    """
    attempts = question.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        question.pop("stalled_since", None)
        question.pop("next_attempt_after", None)
        return

    for att in attempts:
        if isinstance(att, dict) and not att.get("evidence_fp"):
            raw = f"{att.get('method', '')}:{att.get('note', '')}:{att.get('at', '')}"
            att["evidence_fp"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    old_ev = _extract_question_evidence_ids(old_question)
    new_ev = _extract_question_evidence_ids(question) | {
        str(x) for x in (evidence_ids or []) if x
    }
    old_att_len = len((old_question or {}).get("attempts") or []) if old_question else 0
    new_att_len = len(attempts)

    # If new evidence was linked to this question (or backoff was already cleared and no new attempt added)
    if new_ev - old_ev and new_att_len == old_att_len:
        question.pop("stalled_since", None)
        question.pop("next_attempt_after", None)
        return
    if (
        old_question is not None
        and new_att_len == old_att_len
        and not old_question.get("stalled_since")
        and not old_question.get("next_attempt_after")
    ):
        question.pop("stalled_since", None)
        question.pop("next_attempt_after", None)
        return

    # Count trailing 'none' progress attempts
    trailing_none = 0
    first_none_at: str | None = None
    for att in reversed(attempts):
        if isinstance(att, dict) and att.get("progress") == "none":
            trailing_none += 1
            first_none_at = att.get("at")
        else:
            break

    if trailing_none <= 0:
        question.pop("stalled_since", None)
        question.pop("next_attempt_after", None)
        return

    fallback_dt = now or datetime.now(timezone.utc)
    if fallback_dt.tzinfo is None:
        fallback_dt = fallback_dt.replace(tzinfo=timezone.utc)

    last_at_str = attempts[-1].get("at") if isinstance(attempts[-1], dict) else None
    last_dt = _parse_iso_dt(last_at_str, fallback_dt)

    preset_stalled = question.get("stalled_since") or (
        (old_question or {}).get("stalled_since") if old_question else None
    )
    if preset_stalled and first_none_at:
        dt_preset = _parse_iso_dt(preset_stalled, last_dt)
        dt_first = _parse_iso_dt(first_none_at, last_dt)
        stalled_dt = min(dt_preset, dt_first)
        stalled_since_str = (
            preset_stalled if dt_preset <= dt_first else first_none_at
        )
    else:
        stalled_since_str = preset_stalled or first_none_at or _now_iso(last_dt)
        stalled_dt = _parse_iso_dt(stalled_since_str, last_dt)

    stalled_duration_hours = max(
        0.0, (last_dt - stalled_dt).total_seconds() / 3600.0
    )
    hours = min(max(0.5 * stalled_duration_hours, 24.0), 30.0 * 24.0)
    next_dt = last_dt + timedelta(hours=hours)
    if stalled_since_str:
        question["stalled_since"] = stalled_since_str
    question["next_attempt_after"] = _now_iso(next_dt)


def _split_path(path: str) -> list[str]:
    if not path or path == "/":
        return []
    if not path.startswith("/"):
        raise OpError("invalid_path", path, "path must start with '/'")
    return [seg for seg in path.split("/")[1:] if seg != ""]


def _repair_item_dict(
    collection_name: str,
    item: dict[str, Any],
    path_prefix: str,
    warnings: list[dict[str, Any]],
) -> None:
    """Repair known field aliases inside a collection item or nested dict in-place."""
    alias_map = _COLLECTION_FIELD_ALIASES.get(collection_name, {})
    for bad_key in list(item.keys()):
        target_key = alias_map.get(bad_key)
        if target_key and target_key not in item:
            item[target_key] = item.pop(bad_key)
            warnings.append(
                {
                    "code": "field_repaired",
                    "path": f"{path_prefix}/{bad_key}",
                    "message": f"repaired field {bad_key!r} -> {target_key!r}",
                }
            )
    if collection_name == "debates":
        for side_key in ("bull", "bear"):
            if isinstance(item.get(side_key), dict):
                _repair_item_dict(
                    side_key,
                    item[side_key],
                    f"{path_prefix}/{side_key}",
                    warnings,
                )


def _repair_doc_collections(
    doc_key: str,
    doc: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> None:
    """Repair top-level collection aliases and item fields on a full document."""
    doc_aliases = _DOC_PATH_ALIASES.get(doc_key, {})
    for bad_key, target_rel in doc_aliases.items():
        if bad_key in doc:
            val = doc.pop(bad_key)
            if "/" in target_rel:
                p1, p2 = target_rel.split("/", 1)
                sub = doc.setdefault(p1, {})
                if isinstance(sub, dict) and p2 not in sub:
                    sub[p2] = val
            elif target_rel not in doc:
                doc[target_rel] = val
            warnings.append(
                {
                    "code": "field_repaired",
                    "path": f"/{bad_key}",
                    "message": f"repaired path '/{bad_key}' -> '/{target_rel}'",
                }
            )

    for coll_name in _COLLECTION_FIELD_ALIASES:
        arr = doc.get(coll_name)
        if isinstance(arr, list):
            for idx, entry in enumerate(arr):
                if isinstance(entry, dict):
                    _repair_item_dict(
                        coll_name, entry, f"/{coll_name}/{idx}", warnings
                    )
    if doc_key == "agenda" and isinstance(doc.get("watch"), dict):
        evs = doc["watch"].get("events")
        if isinstance(evs, list):
            for idx, entry in enumerate(evs):
                if isinstance(entry, dict):
                    _repair_item_dict(
                        "events", entry, f"/watch/events/{idx}", warnings
                    )


def repair_knowledge_ops(
    doc_key: str,
    base_doc: dict[str, Any] | None,
    ops: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Repair common path/field aliases in ``ops`` and return ``(repaired_ops, warnings)``."""
    repaired_ops = copy.deepcopy(ops)
    warnings: list[dict[str, Any]] = []
    doc_aliases = _DOC_PATH_ALIASES.get(doc_key, {})

    for op in repaired_ops:
        if not isinstance(op, dict):
            continue
        op_name = op.get("op")
        raw_path = str(op.get("path") or "/")
        val = op.get("value")

        if op_name == "create" and isinstance(val, dict):
            _repair_doc_collections(doc_key, val, warnings)
            continue

        segs = _split_path(raw_path)
        if not segs:
            continue

        # 1. Check top-level segment alias (never repair /official)
        top = segs[0]
        if top != "official":
            mapped_top = doc_aliases.get(top)
            if mapped_top is None and isinstance(base_doc, dict) and top not in base_doc:
                close = difflib.get_close_matches(top, list(base_doc.keys()), n=1, cutoff=0.78)
                if close and close[0] != "official":
                    mapped_top = close[0]
            if mapped_top:
                new_top_segs = [s for s in mapped_top.split("/") if s]
                segs = [*new_top_segs, *segs[1:]]
                new_path = "/" + "/".join(segs)
                warnings.append(
                    {
                        "code": "field_repaired",
                        "path": raw_path,
                        "message": f"repaired path {raw_path!r} -> {new_path!r}",
                    }
                )
                op["path"] = new_path
                raw_path = new_path

        # 2. Check leaf field alias on `set` inside a collection item (e.g. /pillars/P1/statement -> /pillars/P1/claim)
        if op_name == "set" and len(segs) >= 3:
            coll_seg = segs[-3]
            leaf_seg = segs[-1]
            alias_map = _COLLECTION_FIELD_ALIASES.get(coll_seg, {})
            mapped_leaf = alias_map.get(leaf_seg)
            if mapped_leaf:
                segs[-1] = mapped_leaf
                new_path = "/" + "/".join(segs)
                warnings.append(
                    {
                        "code": "field_repaired",
                        "path": raw_path,
                        "message": f"repaired field {leaf_seg!r} -> {mapped_leaf!r} at {new_path!r}",
                    }
                )
                op["path"] = new_path
                raw_path = new_path

        # 3. Check item dict fields on append/set/upsert
        target_coll = segs[-1] if op_name in ("append", "append_item", "insert", "upsert_item") else (
            segs[-2] if len(segs) >= 2 else segs[-1]
        )
        if isinstance(val, dict) and target_coll in _COLLECTION_FIELD_ALIASES:
            _repair_item_dict(target_coll, val, raw_path, warnings)

    return repaired_ops, warnings


def _resolve_parent(
    root: dict[str, Any], segments: list[str], full_path: str
) -> tuple[Any, str, str | None]:
    """Traverse ``root`` up to ``segments[:-1]``.

    Returns ``(parent_container, final_segment, parent_id_if_any)``.
    """
    curr: Any = root
    parent_id: str | None = None
    for idx, seg in enumerate(segments[:-1]):
        subpath = "/" + "/".join(segments[: idx + 1])
        if isinstance(curr, dict):
            if seg not in curr:
                # Auto-create intermediate dicts for known optional containers
                if seg in ("thresholds", "watch", "identifiers", "autonomy", "linked"):
                    curr[seg] = {}
                else:
                    raise OpError(
                        "path_not_found",
                        subpath,
                        f"key {seg!r} not found (available: {sorted(curr.keys())})",
                    )
            curr = curr[seg]
        elif isinstance(curr, list):
            matched = None
            available_ids: list[str] = []
            for item in curr:
                if isinstance(item, dict) and "id" in item:
                    available_ids.append(str(item["id"]))
                    if str(item["id"]) == seg:
                        matched = item
                        break
            if matched is None and seg.isdigit():
                i = int(seg)
                if 0 <= i < len(curr):
                    matched = curr[i]
            if matched is None:
                raise OpError(
                    "path_not_found",
                    subpath,
                    f"item {seg!r} not found in list (available ids: {available_ids})",
                )
            parent_id = matched.get("id") if isinstance(matched, dict) else None
            curr = matched
        else:
            raise OpError(
                "path_not_found",
                subpath,
                f"cannot traverse into {type(curr).__name__}",
            )
    return curr, segments[-1], parent_id


def _find_in_list(
    lst: list[Any], target_id: str, path: str
) -> tuple[int, dict[str, Any]]:
    available: list[str] = []
    for idx, item in enumerate(lst):
        if isinstance(item, dict) and "id" in item:
            available.append(str(item["id"]))
            if str(item["id"]) == target_id:
                return idx, item
    if target_id.isdigit():
        i = int(target_id)
        if 0 <= i < len(lst) and isinstance(lst[i], dict):
            return i, lst[i]
    raise OpError(
        "path_not_found",
        path,
        f"id {target_id!r} not found (available: {available})",
    )


def _check_set_append_only_violation(
    segments: list[str],
    parent: Any,
    last_seg: str,
    value: Any,
    path: str,
) -> None:
    """Enforce hard invariant #2: append-only arrays cannot be truncated or overwritten via set."""
    if isinstance(parent, dict) and last_seg in _APPEND_ONLY_ARRAY_NAMES:
        old_arr = parent.get(last_seg)
        if isinstance(old_arr, list) and old_arr:
            if not isinstance(value, list) or len(value) < len(old_arr):
                raise OpError(
                    "append_only_violation",
                    path,
                    f"append-only array {last_seg!r} cannot be truncated or overwritten",
                )
            for idx, old_item in enumerate(old_arr):
                if value[idx] != old_item:
                    raise OpError(
                        "append_only_violation",
                        f"{path}/{idx}",
                        f"cannot mutate existing item at index {idx} in append-only array {last_seg!r}",
                    )
    for idx, seg in enumerate(segments[:-1]):
        if seg in _APPEND_ONLY_ARRAY_NAMES:
            # Allow one-time backfill of actual/outcome on guidance_history
            if seg == "guidance_history" and last_seg in ("actual", "outcome"):
                return
            raise OpError(
                "append_only_violation",
                path,
                f"cannot overwrite existing entry inside append-only array {seg!r}",
            )


def apply_ops(
    doc_key: str,
    base_doc: dict[str, Any] | None,
    ops: list[dict[str, Any]],
    *,
    reason: str = "",
    now: datetime | None = None,
    evidence_ids: list[str] | None = None,
    verdict_id: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply a sequence of ID-addressed operations to ``base_doc`` and return ``(new_doc, summaries)``."""
    if not ops:
        raise OpError("empty_ops", "/", "ops list cannot be empty")

    repaired_ops, repair_warnings = repair_knowledge_ops(doc_key, base_doc, ops)
    now_str = _now_iso(now)
    summaries: list[str] = [w["message"] for w in repair_warnings]

    if base_doc is None:
        if len(repaired_ops) == 1 and repaired_ops[0].get("op") == "create":
            val = repaired_ops[0].get("value")
            if not isinstance(val, dict):
                raise OpError("invalid_value", "/", "create value must be an object")
            doc = copy.deepcopy(val)
            _ensure_nested_ids_and_defaults(
                doc_key, doc, now=now, old_doc=None, evidence_ids=evidence_ids
            )
            summaries.insert(0, f"create {doc_key}")
            return doc, summaries
        raise OpError(
            "doc_not_initialized",
            "/",
            f"{doc_key} does not exist yet; use op='create' with base_version=null",
        )

    old_snapshot = copy.deepcopy(base_doc)
    doc = copy.deepcopy(base_doc)

    for raw_op in repaired_ops:
        op_name = raw_op.get("op")
        path = str(raw_op.get("path") or "/")
        value = copy.deepcopy(raw_op.get("value"))

        if op_name == "create":
            if not isinstance(value, dict):
                raise OpError("invalid_value", path, "create value must be an object")
            doc = value
            summaries.append(f"create {doc_key}")
            continue

        segments = _split_path(path)
        if not segments:
            raise OpError("invalid_path", path, "operation requires a non-root path")

        parent, last_seg, parent_id = _resolve_parent(doc, segments, path)

        if op_name == "set":
            _check_set_append_only_violation(segments, parent, last_seg, value, path)
            if isinstance(parent, dict):
                old_val = parent.get(last_seg)
                parent[last_seg] = value
                if isinstance(old_val, (str, int, float, bool)) and isinstance(
                    value, (str, int, float, bool)
                ):
                    summaries.append(f"set {path} {old_val}→{value}")
                else:
                    summaries.append(f"set {path}")
            elif isinstance(parent, list):
                idx, existing = _find_in_list(parent, last_seg, path)
                if not isinstance(value, dict):
                    raise OpError(
                        "invalid_value", path, "cannot replace list object with scalar"
                    )
                value["id"] = existing.get("id", last_seg)
                parent[idx] = value
                summaries.append(f"set {path}")
            else:
                raise OpError(
                    "invalid_target",
                    path,
                    f"cannot set on {type(parent).__name__}",
                )

        elif op_name in ("append", "append_item", "insert", "upsert_item"):
            if not isinstance(parent, dict):
                raise OpError(
                    "invalid_target", path, "target container parent must be an object"
                )
            arr = parent.setdefault(last_seg, [])
            if not isinstance(arr, list):
                raise OpError(
                    "invalid_target", path, f"{path} is not an array"
                )
            if isinstance(value, dict):
                item_id = value.get("id")
                if item_id:
                    existing_idx = next(
                        (
                            i
                            for i, x in enumerate(arr)
                            if isinstance(x, dict) and str(x.get("id")) == str(item_id)
                        ),
                        None,
                    )
                    if existing_idx is not None:
                        if op_name == "upsert_item":
                            if last_seg in _APPEND_ONLY_ARRAY_NAMES:
                                raise OpError(
                                    "append_only_violation",
                                    path,
                                    f"cannot overwrite existing item {item_id!r} in append-only array {last_seg!r}",
                                )
                            merged = {**arr[existing_idx], **value}
                            arr[existing_idx] = merged
                            summaries.append(f"upsert {path}/{item_id}")
                            continue
                        raise OpError(
                            "duplicate_id",
                            path,
                            f"item with id {item_id!r} already exists in {path}",
                        )
                else:
                    if last_seg in (
                        "pillars",
                        "confirm",
                        "warn",
                        "break",
                        "evidence",
                        "debates",
                        "drivers",
                        "leading_indicators",
                        "guidance_history",
                        "lessons",
                        "open_questions",
                        "next_validations",
                        "events",
                        "wake_conditions",
                        "pm_handoffs",
                        "versions",
                    ):
                        value["id"] = next_item_id(
                            last_seg,
                            arr,
                            parent_id=parent_id,
                            now=now,
                            root_doc=doc,
                        )
                arr.append(value)
                assigned_id = value.get("id")
                summaries.append(
                    f"{op_name} {path}" + (f" {assigned_id}" if assigned_id else "")
                )
            else:
                arr.append(value)
                summaries.append(f"{op_name} {path}")

        elif op_name in ("retire", "retire_item"):
            if not isinstance(parent, list):
                raise OpError("invalid_target", path, "retire path must name a list item")
            _, item = _find_in_list(parent, last_seg, path)
            old_status = item.get("status")
            retire_reason = (
                (value or {}).get("reason")
                if isinstance(value, dict)
                else (str(value) if value else reason)
            ) or reason or "retired"
            item["status"] = "retired"
            item["retired_at"] = now_str
            item["status_reason"] = retire_reason
            hist = item.setdefault("status_history", [])
            hist_entry: dict[str, Any] = {
                "at": now_str,
                "to": "retired",
                "reason": retire_reason,
            }
            if old_status:
                hist_entry["from"] = old_status
            if evidence_ids:
                hist_entry["evidence_ids"] = list(evidence_ids)
            hist.append(hist_entry)
            summaries.append(f"retire {path}")

        elif op_name == "retract":
            if not isinstance(parent, list):
                raise OpError(
                    "invalid_target", path, "retract path must name a lesson item"
                )
            _, item = _find_in_list(parent, last_seg, path)
            retract_reason = (
                (value or {}).get("reason")
                if isinstance(value, dict)
                else (str(value) if value else reason)
            ) or reason or "retracted"
            item["status"] = "retracted"
            item["retracted"] = {"at": now_str, "reason": retract_reason}
            summaries.append(f"retract {path}")

        elif op_name == "remove":
            # Check if removing from a list by item id
            if isinstance(parent, list):
                array_name = segments[-2] if len(segments) >= 2 else ""
                if array_name in _APPEND_ONLY_ARRAY_NAMES:
                    raise OpError(
                        "append_only_violation",
                        path,
                        f"cannot remove from append-only array {array_name!r}",
                    )
                idx, _ = _find_in_list(parent, last_seg, path)
                parent.pop(idx)
                summaries.append(f"remove {path}")
            elif isinstance(parent, dict) and last_seg in parent:
                if last_seg in _APPEND_ONLY_ARRAY_NAMES:
                    raise OpError(
                        "append_only_violation",
                        path,
                        f"cannot remove append-only field {last_seg!r}",
                    )
                parent.pop(last_seg, None)
                summaries.append(f"remove {path}")
            else:
                raise OpError("path_not_found", path, "target to remove not found")

        else:
            raise OpError(
                "unknown_op",
                path,
                f"unsupported op {op_name!r}; expected create/set/append/insert/retire/retract/remove",
            )

    # Post-processing: maintain automatic histories and server-computed fields
    _maintain_histories_after_ops(
        doc_key,
        old_snapshot,
        doc,
        reason=reason,
        now=now,
        evidence_ids=evidence_ids,
        verdict_id=verdict_id,
    )
    _ensure_nested_ids_and_defaults(
        doc_key,
        doc,
        now=now,
        old_doc=old_snapshot,
        evidence_ids=evidence_ids,
    )
    return doc, summaries


def _maintain_histories_after_ops(
    doc_key: str,
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    *,
    reason: str,
    now: datetime | None = None,
    evidence_ids: list[str] | None = None,
    verdict_id: str | None = None,
) -> None:
    now_str = _now_iso(now)
    eff_reason = reason.strip() or "updated via knowledge_update"

    if doc_key == "thesis":
        # 1. Check house_view change
        old_hv = old_doc.get("house_view")
        new_hv = new_doc.get("house_view")
        if isinstance(old_hv, dict) and isinstance(new_hv, dict):
            old_cmp = {k: v for k, v in old_hv.items() if k != "updated_at"}
            new_cmp = {k: v for k, v in new_hv.items() if k != "updated_at"}
            if old_cmp != new_cmp:
                new_hv["updated_at"] = now_str
                hvh = new_doc.setdefault("house_view_history", [])
                old_hvh_len = len(old_doc.get("house_view_history") or [])
                if len(hvh) == old_hvh_len:
                    entry: dict[str, Any] = {
                        "at": now_str,
                        "from": copy.deepcopy(old_hv),
                        "to": copy.deepcopy(new_hv),
                        "reason": eff_reason,
                    }
                    if evidence_ids:
                        entry["evidence_ids"] = list(evidence_ids)
                    if verdict_id:
                        entry["verdict_id"] = verdict_id
                    hvh.append(entry)

        # 2. Check pillar status changes
        old_pillars = {
            p["id"]: p
            for p in (old_doc.get("pillars") or [])
            if isinstance(p, dict) and "id" in p
        }
        for p in new_doc.get("pillars") or []:
            if not isinstance(p, dict) or "id" not in p:
                continue
            old_p = old_pillars.get(p["id"])
            if old_p and old_p.get("status") != p.get("status"):
                sh = p.setdefault("status_history", [])
                old_sh_len = len(old_p.get("status_history") or [])
                if len(sh) == old_sh_len:
                    sh_entry: dict[str, Any] = {
                        "at": now_str,
                        "from": old_p.get("status"),
                        "to": p.get("status"),
                        "reason": p.get("status_reason") or eff_reason,
                    }
                    if evidence_ids:
                        sh_entry["evidence_ids"] = list(evidence_ids)
                    sh.append(sh_entry)

    elif doc_key == "debate":
        old_debates = {
            d["id"]: d
            for d in (old_doc.get("debates") or [])
            if isinstance(d, dict) and "id" in d
        }
        for d in new_doc.get("debates") or []:
            if not isinstance(d, dict) or "id" not in d:
                continue
            old_d = old_debates.get(d["id"])
            if old_d:
                old_cmp = {
                    k: v
                    for k, v in old_d.items()
                    if k not in ("history", "last_reviewed_at")
                }
                new_cmp = {
                    k: v
                    for k, v in d.items()
                    if k not in ("history", "last_reviewed_at")
                }
                if old_cmp != new_cmp:
                    d["last_reviewed_at"] = now_str
                    hist = d.setdefault("history", [])
                    old_h_len = len(old_d.get("history") or [])
                    if len(hist) == old_h_len:
                        h_entry: dict[str, Any] = {
                            "at": now_str,
                            "change": "updated debate view/status",
                            "reason": eff_reason,
                        }
                        if evidence_ids:
                            h_entry["evidence_ids"] = list(evidence_ids)
                        if verdict_id:
                            h_entry["verdict_id"] = verdict_id
                        hist.append(h_entry)


def diff_documents(
    old_doc: dict[str, Any] | None,
    new_doc: dict[str, Any] | None,
    prefix: str = "",
) -> list[dict[str, Any]]:
    """Compute a JSON path-level diff between two knowledge documents (PK.4 / PK.13)."""
    diffs: list[dict[str, Any]] = []
    if old_doc == new_doc:
        return diffs

    if isinstance(old_doc, dict) and isinstance(new_doc, dict):
        all_keys = list(
            dict.fromkeys([*old_doc.keys(), *new_doc.keys()])
        )
        for k in all_keys:
            subpath = f"{prefix}/{k}"
            if k not in old_doc:
                diffs.append({"path": subpath, "op": "added", "old": None, "new": new_doc[k]})
            elif k not in new_doc:
                diffs.append({"path": subpath, "op": "removed", "old": old_doc[k], "new": None})
            else:
                diffs.extend(diff_documents(old_doc[k], new_doc[k], subpath))
        return diffs

    if isinstance(old_doc, list) and isinstance(new_doc, list):
        # If items are dicts with 'id', match by id
        old_has_ids = all(isinstance(x, dict) and "id" in x for x in old_doc) and bool(old_doc)
        new_has_ids = all(isinstance(x, dict) and "id" in x for x in new_doc) and bool(new_doc)
        if (old_has_ids or new_has_ids) and all(
            isinstance(x, dict) and "id" in x for x in [*old_doc, *new_doc]
        ):
            old_map = {str(x["id"]): x for x in old_doc}
            new_map = {str(x["id"]): x for x in new_doc}
            all_ids = list(dict.fromkeys([*old_map.keys(), *new_map.keys()]))
            for item_id in all_ids:
                subpath = f"{prefix}/{item_id}"
                if item_id not in old_map:
                    diffs.append(
                        {"path": subpath, "op": "added", "old": None, "new": new_map[item_id]}
                    )
                elif item_id not in new_map:
                    diffs.append(
                        {"path": subpath, "op": "removed", "old": old_map[item_id], "new": None}
                    )
                else:
                    diffs.extend(
                        diff_documents(old_map[item_id], new_map[item_id], subpath)
                    )
            return diffs

    diffs.append(
        {"path": prefix or "/", "op": "changed", "old": old_doc, "new": new_doc}
    )
    return diffs
