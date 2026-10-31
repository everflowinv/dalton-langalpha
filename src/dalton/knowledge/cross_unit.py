"""Cross-unit knowledge propagation (sector layer P2).

Two directions, both triggered from the single knowledge write pipeline
(``knowledge.service.apply_update``) after a thesis version commits, both
gated on ``coverage_units.enabled``:

- **Sector pillar changed -> company re-review**: when a sector thesis
  version changes a pillar, every company thesis that ``assumes`` that pillar
  at an older version gets ONE open question (origin ``sector``) in its
  agenda. The company thesis is never edited — adoption is the company
  analyst's explicit act.
- **Company evidence contradicts -> sector update candidate**: when a company
  thesis adds evidence whose ``knowledge_refs`` carry ``relation=contradicts``
  against a sector subject, the sector workspace gets ONE open question
  (origin ``company``) in its agenda as an update candidate.

Hard rules encoded here:

- Dedup is ``(source workspace, doc, version, entry, relation)`` — the
  triggering side's NEW version — so the same event can only ever create one
  item, and a later sector version creates a NEW item (new content deserves a
  new re-review) instead of being swallowed by an old one. This is what stops
  sector and company from waking each other in a loop.
- An item is only written when the ACTOR (the user whose write triggered the
  propagation) has write access to the target workspace — same owner, or an
  existing sharing grant. Anything else is skipped and reported, never forced.
- Every step is best-effort around the caller's committed write: failures are
  collected into the effects list, never raised into the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Mapping

from fastapi import HTTPException

from src.dalton.knowledge.profiles import PROFILE_SECTOR

logger = logging.getLogger(__name__)

RELATION_ASSUMES = "assumes"
RELATION_CONTRADICTS = "contradicts"

# CAS 冲突重试上限（codex r1 #3）：并发写同一目标 agenda 时，输家重读重试。
LINKAGE_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------


def _iter_refs(doc: Mapping[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Yield every knowledge_ref on pillars and evidence entries of a thesis doc."""
    if not isinstance(doc, Mapping):
        return
    for p in doc.get("pillars") or []:
        if isinstance(p, dict):
            for ref in p.get("knowledge_refs") or []:
                if isinstance(ref, dict):
                    yield ref
    for ev in doc.get("evidence") or []:
        if isinstance(ev, dict):
            for ref in ev.get("knowledge_refs") or []:
                if isinstance(ref, dict):
                    yield ref


def ref_matches_subject(
    ref: Mapping[str, Any],
    *,
    unit_id: str | None,
    workspace_id: str | None,
) -> bool:
    """A ref points at this subject when either locator matches.

    ``unit_id`` is the stable identity; ``workspace_id`` also matches so a
    ref written against an unregistered subject still resolves.
    """
    if unit_id and str(ref.get("unit_id") or "").strip() == str(unit_id):
        return True
    if workspace_id and str(ref.get("workspace_id") or "").strip() == str(workspace_id):
        return True
    return False


def source_ref_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    """The dedup key: source workspace + doc + version + entry + relation.

    The version in the key is the SOURCE side's version at trigger time (the
    sector's new version / the company thesis version carrying the evidence),
    never the referencing side's — see the module docstring.
    """
    return (
        str(ref.get("workspace_id") or "").strip(),
        str(ref.get("doc_key") or "").strip(),
        str(ref.get("version_no") or "").strip(),
        str(ref.get("entry_id") or "").strip(),
        str(ref.get("relation") or "").strip(),
    )


def changed_pillar_ids(
    old_doc: Mapping[str, Any] | None, new_doc: Mapping[str, Any] | None
) -> set[str]:
    """Pillar ids whose content differs between the last valid doc and the new one.

    Added, edited and REMOVED pillars all count: a removed/retired pillar is
    as invalidating for an adopting company as an edited one.
    """
    old_pillars = {
        str(p.get("id")): p
        for p in ((old_doc or {}).get("pillars") or [])
        if isinstance(p, dict) and p.get("id")
    }
    new_pillars = {
        str(p.get("id")): p
        for p in ((new_doc or {}).get("pillars") or [])
        if isinstance(p, dict) and p.get("id")
    }
    changed: set[str] = set()
    for pid, p in new_pillars.items():
        if pid not in old_pillars or old_pillars[pid] != p:
            changed.add(pid)
    changed.update(pid for pid in old_pillars if pid not in new_pillars)
    return changed


def new_evidence_entries(
    old_doc: Mapping[str, Any] | None, new_doc: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Evidence entries added by this update (the ledger is append-only, so id-based)."""
    old_ids = {
        str(ev.get("id"))
        for ev in ((old_doc or {}).get("evidence") or [])
        if isinstance(ev, dict) and ev.get("id")
    }
    out: list[dict[str, Any]] = []
    for ev in ((new_doc or {}).get("evidence") or []):
        if isinstance(ev, dict) and ev.get("id") and str(ev["id"]) not in old_ids:
            out.append(ev)
    return out


def question_dedup_keys(q: Mapping[str, Any]) -> set[tuple[Any, ...]]:
    """Every dedup key an existing agenda question answers to.

    A linkage item carries ``source_ref`` (where the trigger came from) and,
    for update candidates, ``target_ref`` (the sector entry being contradicted,
    codex r1 #10). The primary key is the source ref; a contradicts item ALSO
    answers to (source workspace + version + contradicted entry) so that
    several evidences against the same pillar collapse into ONE item.
    """
    keys: set[tuple[Any, ...]] = set()
    src = q.get("source_ref")
    if not isinstance(src, dict):
        return keys
    keys.add(source_ref_key(src))
    tgt = q.get("target_ref")
    if isinstance(tgt, dict):
        pillar_key = (
            str(src.get("workspace_id") or "").strip(),
            str(src.get("doc_key") or "").strip(),
            str(src.get("version_no") or "").strip(),
            str(tgt.get("entry_id") or "").strip(),
            str(src.get("relation") or "").strip(),
        )
        keys.add(pillar_key)
        # A supplementary item is keyed on the pillar key plus a digest of the
        # evidence it carries; deriving that key from the persisted item lets a
        # concurrent retry, rereading after a CAS conflict, see it exists.
        ev = q.get("evidence_ids")
        if isinstance(ev, list) and ev:
            keys.add((*pillar_key, _missing_evidence_digest([str(e) for e in ev])))
    return keys


def update_candidate_dedup_key(
    source_ref: Mapping[str, Any], target_ref: Mapping[str, Any]
) -> tuple[Any, ...]:
    """(source workspace + doc + version + contradicted entry + relation)."""
    return (
        str(source_ref.get("workspace_id") or "").strip(),
        str(source_ref.get("doc_key") or "").strip(),
        str(source_ref.get("version_no") or "").strip(),
        str(target_ref.get("entry_id") or "").strip(),
        str(source_ref.get("relation") or "").strip(),
    )


def _agenda_has_key(content: Mapping[str, Any] | None, key: tuple[Any, ...]) -> bool:
    if not isinstance(content, Mapping):
        return False
    for q in content.get("open_questions") or []:
        if isinstance(q, dict) and key in question_dedup_keys(q):
            return True
    return False


async def _existing_evidence_for_key(
    target_workspace_id: str, key: tuple[Any, ...]
) -> set[str]:
    """Evidence ids already carried by every agenda question answering to ``key``.

    ``evidence_ids`` is a long-standing optional field on agenda questions;
    entries written before linkage items started filling it simply contribute
    nothing, which the coverage check treats as "not covered".
    """
    from src.dalton.database import knowledge as db_knowledge

    latest = await db_knowledge.latest_version(target_workspace_id, "agenda")
    content = (latest or {}).get("content") if isinstance(latest, dict) else None
    covered: set[str] = set()
    if not isinstance(content, Mapping):
        return covered
    for q in content.get("open_questions") or []:
        if isinstance(q, dict) and key in question_dedup_keys(q):
            for e in q.get("evidence_ids") or []:
                covered.add(str(e))
    return covered


def _missing_evidence_digest(missing: list[str]) -> str:
    """Stable short digest of a missing-evidence set (supplementary dedup)."""
    import hashlib

    return hashlib.sha1("\n".join(sorted(missing)).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# DB scan (live parse, no index table — see P2 reply for the tradeoff)
# ---------------------------------------------------------------------------


async def find_thesis_refs_to_subject(
    *,
    unit_id: str | None,
    workspace_id: str,
    relation: str,
    entry_ids: set[str],
    before_version: int,
) -> dict[str, list[dict[str, Any]]]:
    """Workspaces whose latest valid thesis references this subject's entries.

    Latest-first, filter-second (codex r1 #7): the subquery takes each
    workspace's newest valid thesis version, and ONLY that version is then
    checked for the needles — a company that deleted its references in v2 must
    not be re-selected through its own v1 history. The needles stay a
    parameterized ``strpos`` (never a LIKE pattern). Returns
    ``{workspace_id: [matching refs]}``; the subject's own workspace is
    excluded (self-references do not propagate).
    """
    from src.server.database.pool import get_db_connection

    needles = [s for s in (unit_id, workspace_id) if s]
    if not needles or not entry_ids:
        return {}
    from psycopg.rows import dict_row

    out: dict[str, list[dict[str, Any]]] = {}
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id, version_no, content
                FROM (
                    SELECT DISTINCT ON (workspace_id)
                           workspace_id::text AS workspace_id, version_no, content
                    FROM dalton_knowledge_versions
                    WHERE doc_key = 'thesis'
                      AND schema_valid = TRUE
                    ORDER BY workspace_id, version_no DESC
                ) latest_thesis
                WHERE strpos(latest_thesis.content::text, %s) > 0
                   OR strpos(latest_thesis.content::text, %s) > 0
                """,
                (needles[0], needles[-1]),
            )
            rows = await cur.fetchall()
    for row in rows:
        ws = str(row.get("workspace_id") or "")
        if not ws or ws == str(workspace_id):
            continue
        matches = [
            ref
            for ref in _iter_refs(row.get("content"))
            if str(ref.get("relation") or "") == relation
            and str(ref.get("doc_key") or "") == "thesis"
            and str(ref.get("entry_id") or "") in entry_ids
            and _version_no(ref) is not None
            and _version_no(ref) < before_version
            and ref_matches_subject(ref, unit_id=unit_id, workspace_id=workspace_id)
        ]
        if matches:
            out[ws] = matches
    return out


def _version_no(ref: Mapping[str, Any]) -> int | None:
    try:
        return int(ref.get("version_no"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Permission + delivery
# ---------------------------------------------------------------------------


async def actor_can_write_workspace(user_id: str, workspace_id: str) -> bool:
    """Same owner, or an existing sharing grant — exactly the CoS write rule."""
    from src.dalton.auth import verify_workspace_access

    try:
        await verify_workspace_access(workspace_id, user_id, need="write", channel="cos")
    except HTTPException:
        return False
    except Exception as exc:
        logger.warning(
            "[DALTON_CROSS_UNIT] access check failed for %s on %s: %s",
            user_id,
            workspace_id,
            exc,
        )
        return False
    return True


async def _append_linkage_question(
    *,
    actor_user_id: str,
    target_workspace_id: str,
    question: str,
    why: str,
    origin: str,
    source_ref: dict[str, Any],
    target_ref: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
    dedup_key: tuple[Any, ...] | None = None,
    value: str = "high",
    urgency: str = "this_week",
) -> dict[str, Any]:
    """Append one deduped open question to the target agenda, CAS-protected.

    The write goes through ``apply_update(source='tool')`` so ``insert_version``
    enforces compare-and-swap on the agenda version; a conflict means another
    writer (a concurrent propagation, or the company's own analyst) moved the
    doc — re-read, re-dedup against what actually landed, retry, up to
    ``LINKAGE_MAX_ATTEMPTS`` (codex r1 #3). Permission is re-checked before
    EVERY attempt, first and retries alike (codex r2 #6): a share revoked
    between attempts must stop the write, not let it land via
    ``skip_access_check``. A non-CAS failure (typically the target has no
    agenda document yet) is returned as ``failed`` for the caller to persist
    as a retryable gap.
    """
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge.service import apply_update

    key = dedup_key if dedup_key is not None else source_ref_key(source_ref)
    for _attempt in range(LINKAGE_MAX_ATTEMPTS):
        if not await actor_can_write_workspace(actor_user_id, target_workspace_id):
            return {"status": "skipped_no_access"}
        latest = await db_knowledge.latest_version(target_workspace_id, "agenda")
        content = (latest or {}).get("content") if isinstance(latest, dict) else None
        if _agenda_has_key(content, key):
            return {"status": "skipped_duplicate"}
        item_value: dict[str, Any] = {
            "question": question,
            "value": value,
            "urgency": urgency,
            "origin": origin,
            "owner": "analyst",
            "status": "open",
            "why": why,
            "source_ref": source_ref,
        }
        if target_ref is not None:
            item_value["target_ref"] = target_ref
        if evidence_ids:
            item_value["evidence_ids"] = list(evidence_ids)
        res = await apply_update(
            user_id=actor_user_id,
            workspace_id=target_workspace_id,
            doc_key="agenda",
            ops=[
                {
                    "op": "append_item",
                    "path": "/open_questions",
                    "value": item_value,
                }
            ],
            reason=f"行业层联动（{origin}）：{why[:120]}",
            base_version=latest["version_no"] if isinstance(latest, dict) else None,
            source="tool",
            backend=None,
            skip_access_check=True,
            skip_linkage_retry=True,
        )
        if isinstance(res, dict) and res.get("ok"):
            return {"status": "created", "version_no": res.get("version_no")}
        if isinstance(res, dict) and res.get("error") == "version_conflict":
            continue
        return {
            "status": "failed",
            "error": (res or {}).get("error") if isinstance(res, dict) else "unknown",
            "message": str((res or {}).get("message") or "") if isinstance(res, dict) else "",
        }
    return {
        "status": "failed",
        "error": "version_conflict",
        "message": f"{LINKAGE_MAX_ATTEMPTS} 次 CAS 冲突后放弃，已记为可重试缺口",
    }


# ---------------------------------------------------------------------------
# The two propagation directions
# ---------------------------------------------------------------------------


async def _propagate_sector_pillar_change(
    *,
    user_id: str,
    workspace_id: str,
    old_doc: Mapping[str, Any] | None,
    new_doc: Mapping[str, Any],
    new_version_no: int,
    sector_unit: Mapping[str, Any],
) -> list[dict[str, Any]]:
    changed = changed_pillar_ids(old_doc, new_doc)
    if not changed:
        return []
    unit_id = str(sector_unit.get("unit_id") or "")
    sector_name = str(sector_unit.get("display_name") or sector_unit.get("slug") or "")
    referers = await find_thesis_refs_to_subject(
        unit_id=unit_id or None,
        workspace_id=workspace_id,
        relation=RELATION_ASSUMES,
        entry_ids=changed,
        before_version=new_version_no,
    )
    effects: list[dict[str, Any]] = []
    for target_ws, refs in sorted(referers.items()):
        changed_here = sorted({str(r.get("entry_id")) for r in refs})
        base_ref: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "doc_key": "thesis",
            "version_no": int(new_version_no),
            "relation": RELATION_ASSUMES,
        }
        if unit_id:
            base_ref["unit_id"] = unit_id
        # One item per changed pillar: each pillar carries its own re-review
        # trail (dedup key includes the entry id), and a company that assumed
        # two changed pillars gets two precisely-scoped questions.
        for pid in changed_here:
            question = (
                f"待复核：行业支柱 {pid}（{sector_name}）已更新到 v{new_version_no}，"
                "本公司论点仍采用旧版本（assumes 引用）。请核验新支柱内容后决定是否采纳；"
                "采纳时更新 thesis 对应支柱的 knowledge_refs 指向新版本，不采纳则记录理由。"
            )
            why = f"行业 {sector_name} thesis v{new_version_no} 改变了支柱 {pid}，本公司有 assumes 引用"
            effect: dict[str, Any] = {
                "kind": "re_review",
                "target_workspace_id": target_ws,
                "sector_unit_id": unit_id,
                "pillars": [pid],
                # 供失败时持久化为可重试缺口（codex r1 #4 / r2 #2）
                "origin": "sector",
                "question": question,
                "source_ref": {**base_ref, "entry_id": pid},
            }
            # 单个目标的失败只记 failed effect，不中止其余目标（codex r2 #2）；
            # effect 带完整信息，持久化方有 question / 锚点可补发。
            try:
                if not await actor_can_write_workspace(user_id, target_ws):
                    effect["status"] = "skipped_no_access"
                    effects.append(effect)
                    continue
                res = await _append_linkage_question(
                    actor_user_id=user_id,
                    target_workspace_id=target_ws,
                    question=question,
                    why=why,
                    origin="sector",
                    source_ref=effect["source_ref"],
                )
                effect["status"] = res["status"]
                if res.get("error"):
                    effect["error"] = res["error"]
                if res.get("version_no"):
                    effect["agenda_version_no"] = res["version_no"]
            except Exception as exc:
                effect["status"] = "failed"
                effect["error"] = str(exc) or type(exc).__name__
                logger.warning(
                    "[DALTON_CROSS_UNIT] linkage to %s failed: %s", target_ws, exc
                )
            effects.append(effect)
    return effects


async def _propagate_company_contradictions(
    *,
    user_id: str,
    workspace_id: str,
    old_doc: Mapping[str, Any] | None,
    new_doc: Mapping[str, Any],
    new_version_no: int,
) -> list[dict[str, Any]]:
    from src.dalton.database import coverage_units as cu_db

    # codex r4 #2：先按（引用的 workspace_id + 被反驳的支柱）把全部反证
    # 证据分组，每组只解析一次目标——解析失败整组记一条含全部 evidence_ids
    # 的可重放 gap，成功则发一条含全部 evidence_ids 的通知。同一组绝不拆成
    # "一部分成功、一部分 gap"（那样 retry 会因支柱级去重键误判 duplicate，
    # 漏掉失败那部分的证据）。引用 schema 必带 workspace_id，分组目标因此
    # 总可确定；连证据 id 都没有的引用才落进不可重放分支。
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in new_evidence_entries(old_doc, new_doc):
        ev_id = str(ev.get("id") or "")
        for ref in ev.get("knowledge_refs") or []:
            if not isinstance(ref, dict) or str(ref.get("relation") or "") != RELATION_CONTRADICTS:
                continue
            ref_unit = str(ref.get("unit_id") or "").strip().lower() or None
            # UUIDs compare case-insensitively; group on the canonical form so
            # two spellings of one workspace stay one group.
            ref_ws = str(ref.get("workspace_id") or "").strip().lower()
            if not ref_unit and not ref_ws:
                continue
            pillar = str(ref.get("entry_id") or "").strip()
            # Group by the cited unit when there is one: refs to one unit by
            # its old and new workspace id are one target, so they resolve
            # once and succeed or fail together (a split would leave a gap the
            # pillar-level retry key then drops as a duplicate). Without a
            # unit, the workspace is the target.
            group = groups.setdefault(
                (f"unit:{ref_unit}" if ref_unit else f"ws:{ref_ws}", pillar),
                {
                    "ref_ws": ref_ws,
                    "pillar": pillar,
                    "ref_unit": ref_unit,
                    "evidence_ids": [],
                    "cited_versions": [],
                },
            )
            if ref_unit and not group["ref_unit"]:
                group["ref_unit"] = ref_unit
            if ev_id and ev_id not in group["evidence_ids"]:
                group["evidence_ids"].append(ev_id)
            group["cited_versions"].append(_version_no(ref))

    targets: dict[tuple[str, str], dict[str, Any]] = {}
    effects: list[dict[str, Any]] = []
    for (_group_key, pillar), group in groups.items():
        ref_ws = group["ref_ws"]
        evidence_ids = group["evidence_ids"]
        # 每组只解析一次目标
        try:
            sector_unit = None
            if group["ref_unit"]:
                sector_unit = await cu_db.get_coverage_unit(group["ref_unit"])
            else:
                sector_unit = await cu_db.get_active_unit_by_workspace(ref_ws)
            if (
                sector_unit is None
                or str(sector_unit.get("kind")) != "sector"
                or str(sector_unit.get("status") or "active") != "active"
            ):
                continue
            bound_ws = str(sector_unit.get("workspace_id") or "").strip()
            if not bound_ws:
                continue
        except Exception as exc:
            # 目标取自引用的 workspace_id，整组可重放（codex r4 #2：含全部
            # evidence_ids，retry 补发时一并带上）。
            if not ref_ws or not evidence_ids:
                logger.error(
                    "[DALTON_CROSS_UNIT] preparation failed for contradicts group "
                    "(pillar=%s, no resolvable target, NOT recorded as retryable): %s",
                    pillar,
                    exc,
                )
                effects.append(
                    {
                        "kind": "preparation_error",
                        "status": "failed",
                        "error": str(exc) or type(exc).__name__,
                        "replayable": False,
                        "entry_id": pillar,
                        "evidence_ids": evidence_ids,
                    }
                )
                continue
            source_ref = {
                "workspace_id": str(workspace_id),
                "doc_key": "thesis",
                "version_no": int(new_version_no),
                "entry_id": evidence_ids[0],
                "relation": RELATION_CONTRADICTS,
            }
            target_ref = {
                "workspace_id": ref_ws,
                "doc_key": "thesis",
                "version_no": next(
                    (v for v in group["cited_versions"] if v is not None),
                    None,
                )
                or int(new_version_no),
                "entry_id": pillar,
                "relation": RELATION_CONTRADICTS,
            }
            if group["ref_unit"]:
                target_ref["unit_id"] = group["ref_unit"]
            ev_list = "、".join(evidence_ids)
            effects.append(
                {
                    "kind": "update_candidate",
                    "target_workspace_id": ref_ws,
                    "sector_unit_id": group["ref_unit"] or "",
                    "evidence_ids": evidence_ids,
                    "entry_id": pillar,
                    "origin": "company",
                    "question": (
                        f"更新候选：公司证据 {ev_list}（thesis v{new_version_no}）"
                        f"与行业条目 {pillar or '（未指名）'} 相矛盾（准备阶段解析失败，"
                        f"原因：{exc}）。请核验后决定是否更新行业结论。"
                    ),
                    "source_ref": source_ref,
                    "target_ref": target_ref,
                    "status": "failed",
                    "error": str(exc) or type(exc).__name__,
                }
            )
            continue
        # Two groups can resolve to one bound workspace (a unit rebound to a new
        # workspace, cited by its old and new id): merge, never overwrite, so no
        # evidence is dropped from the one notice that target gets.
        existing = targets.get((bound_ws, pillar))
        if existing is None:
            targets[(bound_ws, pillar)] = {
                "sector_unit": sector_unit,
                "bound_ws": bound_ws,
                "pillar": pillar,
                "evidence_ids": list(evidence_ids),
                "cited_versions": list(group["cited_versions"]),
            }
        else:
            for eid in evidence_ids:
                if eid not in existing["evidence_ids"]:
                    existing["evidence_ids"].append(eid)
            for cv in group["cited_versions"]:
                if cv not in existing["cited_versions"]:
                    existing["cited_versions"].append(cv)

    # 公司自己的覆盖单元（有则带进来源锚点；未登记的兼容公司省略 unit_id）。
    # 只有真的有反证目标时才查——普通公司 thesis 更新保持零注册表查询；
    # 查询失败只是少一个可选锚点字段，不影响传播（codex r3 #3）。
    company_unit_id = ""
    if targets:
        try:
            company_unit = await cu_db.get_active_unit_by_workspace(workspace_id)
            if company_unit is not None and str(company_unit.get("kind")) == "company":
                company_unit_id = str(company_unit.get("unit_id") or "")
        except Exception as exc:
            logger.warning(
                "[DALTON_CROSS_UNIT] company unit lookup failed for %s; "
                "source_ref will omit unit_id: %s",
                workspace_id,
                exc,
            )

    for (bound_ws, pillar), tgt in sorted(targets.items()):
        sector_unit = tgt["sector_unit"]
        sector_name = str(sector_unit.get("display_name") or sector_unit.get("slug") or "")
        unit_id = str(sector_unit.get("unit_id") or "")
        evidence_ids = tgt["evidence_ids"]
        if not evidence_ids:
            continue
        ev_list = "、".join(evidence_ids)
        cited_version = next((v for v in tgt["cited_versions"] if v is not None), None)
        # 来源锚点指向公司侧的证据条目（E-…）；被反驳的行业支柱放 target_ref，
        # 去重键 = 公司工作区 + 版本 + 被反驳的支柱（codex r1 #10）。
        source_ref: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "doc_key": "thesis",
            "version_no": int(new_version_no),
            "entry_id": evidence_ids[0],
            "relation": RELATION_CONTRADICTS,
        }
        if company_unit_id:
            source_ref["unit_id"] = company_unit_id
        target_ref: dict[str, Any] = {
            "workspace_id": bound_ws,
            "doc_key": "thesis",
            "version_no": cited_version or int(new_version_no),
            "entry_id": pillar,
            "relation": RELATION_CONTRADICTS,
        }
        if unit_id:
            target_ref["unit_id"] = unit_id
        question = (
            f"更新候选：公司证据 {ev_list}（thesis v{new_version_no}）与行业条目 "
            f"{pillar or '（未指名）'} 相矛盾。请核验该证据的代表性与适用范围后决定是否"
            "更新行业结论；不得直接平均公司观点。"
        )
        effect: dict[str, Any] = {
            "kind": "update_candidate",
            "target_workspace_id": bound_ws,
            "sector_unit_id": unit_id,
            "evidence_ids": evidence_ids,
            "entry_id": pillar,
            "origin": "company",
            "question": question,
            "source_ref": source_ref,
            "target_ref": target_ref,
        }
        # 逐条检查权限（codex r1 #9）+ 单条失败不中止其余目标（codex r2 #2）；
        # 失败 effect 带完整信息（question/锚点/evidence_ids）供持久化补发。
        try:
            if not await actor_can_write_workspace(user_id, bound_ws):
                effect["status"] = "skipped_no_access"
                effects.append(effect)
                continue
            res = await _append_linkage_question(
                actor_user_id=user_id,
                target_workspace_id=bound_ws,
                question=question,
                why=f"公司证据 {ev_list} 对行业 {sector_name} 条目 {pillar or '（未指名）'} 提出 contradicts 引用",
                origin="company",
                source_ref=source_ref,
                target_ref=target_ref,
                evidence_ids=evidence_ids,
                dedup_key=update_candidate_dedup_key(source_ref, target_ref),
            )
            effect["status"] = res["status"]
            if res.get("error"):
                effect["error"] = res["error"]
            if res.get("version_no"):
                effect["agenda_version_no"] = res["version_no"]
        except Exception as exc:
            effect["status"] = "failed"
            effect["error"] = str(exc) or type(exc).__name__
            logger.warning(
                "[DALTON_CROSS_UNIT] linkage to %s failed: %s", bound_ws, exc
            )
        effects.append(effect)
    return effects


# ---------------------------------------------------------------------------
# Failure persistence + retry (codex r1 #4)
# ---------------------------------------------------------------------------


def _retry_payload_from_effect(effect: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "origin": effect.get("origin"),
        "source_ref": effect.get("source_ref"),
        "target_ref": effect.get("target_ref"),
        "evidence_ids": effect.get("evidence_ids"),
        "error": effect.get("error"),
        "effect_kind": effect.get("kind"),
    }


def _gap_question_from_effect(effect: Mapping[str, Any]) -> str:
    """The gap row's question — the per-target effect's own wording.

    Only replayable per-target effects reach this function; they always carry
    a question. Preparation-level failures have no per-target context and are
    refused before this point (codex r3 #3) — no synthetic "gap" wording that
    would make an unreplayable row look retryable.
    """
    return str(effect.get("question") or "").strip()


async def persist_failed_linkage(actor_user_id: str, effect: Mapping[str, Any]) -> bool:
    """Best-effort: record one failed linkage as a retryable ``gap`` row (d026).

    Returns whether the row actually landed — the caller must NOT claim "已记
    为 gap" when this is False (codex r2 #2). Never raises into the knowledge
    write; a ledger failure is logged and reported through the return value.
    An effect marked ``replayable=False`` (preparation failure, no resolvable
    target) is refused: a gap row without a replayable context would answer
    every retry with ``skipped_malformed_row`` (codex r3 #3).
    """
    from src.dalton.database import cross_unit_requests as cur_db

    if effect.get("replayable") is False:
        logger.error(
            "[DALTON_CROSS_UNIT] refusing to persist an unreplayable failure "
            "(kind=%s, error=%s)",
            effect.get("kind"),
            effect.get("error"),
        )
        return False
    try:
        await cur_db.insert_request(
            owner_user_id=actor_user_id,
            question=_gap_question_from_effect(effect),
            sector_unit_id=str(effect.get("sector_unit_id") or "") or None,
            target_workspace_id=str(effect.get("target_workspace_id") or "") or None,
            kind="linkage_retry",
            payload=_retry_payload_from_effect(effect),
        )
        return True
    except Exception as exc:
        logger.error(
            "[DALTON_CROSS_UNIT] could not persist linkage gap for %s (%s): %s",
            effect.get("target_workspace_id"),
            effect.get("error"),
            exc,
        )
        return False


async def retry_linkage_rows(
    user_id: str,
    *,
    target_workspace_id: str | None = None,
    limit: int = 20,
    cursor: tuple[Any, str] | None = None,
) -> dict[str, Any]:
    """Retry this caller's pending linkage gaps in age order (oldest first).

    Director-driven only (``dalton_cross_unit_request action="retry"`` from
    the Home thread): the append writes with ``backend=None`` and the TARGET's
    next turn start applies the pending files via ``apply_pending`` — before
    that turn's own end-of-turn ``detect_direct_edits`` runs, so the retried
    item can never be mis-read as a direct edit (codex r2 #1 ordering).

    Selection (codex r2 #3): the target filter is pushed down to SQL, rows
    are scanned ``created_at ASC`` (oldest first) with a keyset cursor
    (``(created_at, request_id)`` of the previous batch's last row) and a
    caller-supplied batch size, so a pile of newer unrecoverable gaps can
    never starve an older recoverable one. Returns
    ``{"results": [...], "remaining": <gap count for this filter>,
    "next_cursor": <(created_at, request_id) of this batch's last row or
    None when exhausted>}`` — rows that stay ``skipped_no_access`` do not
    leave the gap state, so the next batch must advance with the cursor.
    Permission is re-checked per row against the CURRENT state; a still-failing
    row keeps its ``gap`` status. Owner-scoped: a colleague's gaps stay in
    their ledger.
    """
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.database import cross_unit_requests as cur_db

    batch_limit = max(1, min(int(limit), 100))
    rows, _remaining_before = await cur_db.list_linkage_retry_batch(
        user_id,
        target_workspace_id=target_workspace_id,
        limit=batch_limit,
        cursor=cursor,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        row_target = str(row.get("target_workspace_id") or "") or None
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        source_ref = payload.get("source_ref")
        if not isinstance(source_ref, dict) or not row_target:
            results.append(
                {"request_id": row.get("request_id"), "status": "skipped_malformed_row"}
            )
            continue
        target_ref = payload.get("target_ref")
        # codex r7 #1：带单元身份的 gap，投递前按单元重新解析当前有效绑定——
        # 单元可能已换绑；gap 里记的工作区是落 gap 时的快照，照投会写进旧
        # 工作区并把 gap 标成已完成。解析失败/单元不再是 active sector →
        # 保留 gap 并写明原因；解析成功则把投递目标与 target_ref.workspace_id
        # 同步到当前绑定。
        if isinstance(target_ref, dict):
            target_unit_id = str(target_ref.get("unit_id") or "").strip()
            if target_unit_id:
                try:
                    unit_row = await cu_db.get_coverage_unit(target_unit_id)
                except Exception as exc:
                    results.append(
                        {
                            "request_id": row.get("request_id"),
                            "target_workspace_id": row_target,
                            "status": "failed",
                            "error": f"单元绑定解析失败，保留 gap：{exc}",
                        }
                    )
                    continue
                if (
                    unit_row is None
                    or str(unit_row.get("kind")) != "sector"
                    or str(unit_row.get("status") or "active") != "active"
                    or not str(unit_row.get("workspace_id") or "").strip()
                ):
                    results.append(
                        {
                            "request_id": row.get("request_id"),
                            "target_workspace_id": row_target,
                            "status": "failed",
                            "error": "单元已不是 active 的 sector（或已无绑定工作区），保留 gap",
                        }
                    )
                    continue
                current_ws = str(unit_row["workspace_id"]).strip()
                if current_ws != row_target:
                    row_target = current_ws
                    target_ref = {**target_ref, "workspace_id": current_ws}
        dedup_key: tuple[Any, ...] | None = None
        if isinstance(target_ref, dict):
            dedup_key = update_candidate_dedup_key(source_ref, target_ref)
        if not await actor_can_write_workspace(user_id, row_target):
            results.append({"request_id": row.get("request_id"), "status": "skipped_no_access"})
            continue
        gap_evidence = [str(e) for e in (payload.get("evidence_ids") or [])]
        try:
            res = await _append_linkage_question(
                actor_user_id=user_id,
                target_workspace_id=row_target,
                question=str(row.get("question") or ""),
                why=f"重试联动缺口（原失败原因：{payload.get('error') or 'unknown'}）",
                origin=str(payload.get("origin") or "sector"),
                source_ref=dict(source_ref),
                target_ref=dict(target_ref) if isinstance(target_ref, dict) else None,
                evidence_ids=gap_evidence or None,
                dedup_key=dedup_key,
            )
        except Exception as exc:
            res = {"status": "failed", "error": str(exc) or type(exc).__name__}
        # codex r7 #2：支柱级去重键命中 ≠ gap 完成。已有待办可能只覆盖了
        # gap 证据的一部分（分组边角下先投递的那部分）——缺的证据补一条
        # "补充反证"待办（独立去重键 = 原键 + 缺失集合摘要），补齐后才把
        # gap 标为 fulfilled。
        supplemented = False
        missing: list[str] = []
        if res["status"] == "skipped_duplicate":
            try:
                covered = (
                    await _existing_evidence_for_key(row_target, dedup_key)
                    if dedup_key is not None
                    else set()
                )
                missing = [e for e in gap_evidence if e not in covered]
            except Exception as exc:
                # One row's coverage read failing keeps that gap open and lets
                # the batch go on; it is not a reason to stop every later row.
                res = {
                    "status": "failed",
                    "error": f"coverage check failed, gap kept: {exc or type(exc).__name__}",
                }
            if missing:
                pillar = (
                    str(target_ref.get("entry_id") or "")
                    if isinstance(target_ref, dict)
                    else ""
                )
                try:
                    supp = await _append_linkage_question(
                        actor_user_id=user_id,
                        target_workspace_id=row_target,
                        question=(
                            f"补充反证：公司证据 {'、'.join(missing)}（thesis "
                            f"v{source_ref.get('version_no')}）同样与行业条目 "
                            f"{pillar or '（未指名）'} 相矛盾——此前同类通知未包含这部分证据。"
                            "请一并核验后决定是否更新行业结论。"
                        ),
                        why=f"补充此前通知缺失的反证证据：{'、'.join(missing)}",
                        origin=str(payload.get("origin") or "sector"),
                        source_ref=dict(source_ref),
                        target_ref=dict(target_ref) if isinstance(target_ref, dict) else None,
                        evidence_ids=missing,
                        dedup_key=(
                            *(dedup_key or source_ref_key(source_ref)),
                            _missing_evidence_digest(missing),
                        ),
                    )
                except Exception as exc:
                    supp = {"status": "failed", "error": str(exc) or type(exc).__name__}
                if supp["status"] in ("created", "skipped_duplicate"):
                    supplemented = True
                else:
                    res = supp
        entry: dict[str, Any] = {
            "request_id": row.get("request_id"),
            "target_workspace_id": row_target,
            "status": res["status"],
        }
        if res.get("error"):
            entry["error"] = res["error"]
        if supplemented:
            entry["supplemented"] = True
        results.append(entry)
        if res["status"] in ("created", "skipped_duplicate"):
            try:
                await cur_db.update_request(
                    str(row.get("request_id") or ""),
                    user_id,
                    status="fulfilled",
                    target_workspace_id=row_target,
                    result=[
                        {
                            "retried": True,
                            "agenda_version_no": res.get("version_no"),
                            "dedup": res["status"] == "skipped_duplicate",
                            "supplemented": supplemented,
                        }
                    ],
                )
            except Exception as exc:
                # 已补发但台账没标成 fulfilled：行仍是 gap，明示给调用方
                # （codex r3 #4），下一轮会被再次扫到并因去重而幂等。
                entry["bookkeeping_failed"] = True
                entry["note"] = "已补发但台账更新失败"
                logger.warning("[DALTON_CROSS_UNIT] retry bookkeeping failed: %s", exc)
    # remaining 以重新查询的计数为准（codex r3 #4）：成功记账的行离开了
    # gap；记账失败的行仍算未处理。同过滤、同游标，limit=1 只为拿计数。
    _, remaining = await cur_db.list_linkage_retry_batch(
        user_id,
        target_workspace_id=target_workspace_id,
        limit=1,
        cursor=cursor,
    )
    # 不足一批即已扫尽：不产游标，调用方据 remaining/空批收尾。
    next_cursor = None
    if rows and len(rows) >= batch_limit:
        last = rows[-1]
        last_at = last.get("created_at")
        if hasattr(last_at, "isoformat"):
            last_at = last_at.isoformat()
        next_cursor = (last_at, str(last.get("request_id") or ""))
    return {"results": results, "remaining": remaining, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Entry point (called from knowledge.service.apply_update)
# ---------------------------------------------------------------------------


async def propagate_thesis_change(
    *,
    user_id: str,
    workspace_id: str,
    old_doc: Mapping[str, Any] | None,
    new_doc: Mapping[str, Any],
    new_version_no: int,
    profile: str,
) -> list[dict[str, Any]]:
    """Run the matching propagation for one committed thesis version.

    The flag gate lives in the caller (``apply_update``) and is re-checked
    here — the company path must stay query-free with the flag off whatever
    future call sites appear. Never raises into the caller: an unexpected
    error is logged and returned as a failed effect so the (already
    committed) knowledge write is never rolled back by a linkage problem.
    """
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.settings import is_coverage_units_enabled

    if not is_coverage_units_enabled():
        return []
    try:
        if profile == PROFILE_SECTOR:
            sector_unit = await cu_db.get_active_unit_by_workspace(workspace_id)
            if sector_unit is None or str(sector_unit.get("kind")) != "sector":
                return []
            return await _propagate_sector_pillar_change(
                user_id=user_id,
                workspace_id=workspace_id,
                old_doc=old_doc,
                new_doc=new_doc,
                new_version_no=new_version_no,
                sector_unit=sector_unit,
            )
        return await _propagate_company_contradictions(
            user_id=user_id,
            workspace_id=workspace_id,
            old_doc=old_doc,
            new_doc=new_doc,
            new_version_no=new_version_no,
        )
    except Exception as exc:
        # 准备阶段（扫描/单元解析）失败时没有任何逐目标上下文，不可重放：
        # 不进 gap 台账，只记 error 日志，由调用方在 message 里如实告知
        # "重新保存该文档可再次触发"（codex r3 #3）。
        logger.error(
            "[DALTON_CROSS_UNIT] propagation preparation failed for %s thesis v%s "
            "(no resolvable target, NOT recorded as retryable): %s",
            workspace_id,
            new_version_no,
            exc,
        )
        return [
            {
                "kind": "preparation_error",
                "status": "failed",
                "error": str(exc) or type(exc).__name__,
                "replayable": False,
            }
        ]
