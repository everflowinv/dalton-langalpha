"""Coverage-unit view assembly shared by the director board and the HTTP API.

The ``units`` view (sector layer P2) used to live inline in
``build_coverage_board``; the P4 coverage API needs the same enumeration, so
the block moved here verbatim and the board delegates. Colleague visibility,
member rendering, and the automations preload have exactly one implementation
— board, list endpoint and detail endpoint all read through these functions,
so the three surfaces cannot drift on who sees what.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.dalton.database.coverage_units import (
    get_coverage_unit,
    load_active_units_by_workspace,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


async def load_dalton_automations_by_user(
    cur: Any, user_id: str | None
) -> dict[str, list[dict[str, Any]]]:
    """Prefetch the user's ``dalton-*`` automation rows (identity fallback input).

    Same filter and order as tracking's own rule: SQL only prefilters on the
    ``dalton-`` prefix, Python's ``strip().lower().startswith`` decides —
    paused rows count too, because a paused tracking task is still verified
    tracking information.
    """
    autos_by_user: dict[str, list[dict[str, Any]]] = {}
    # 与 tracking 的筛选和顺序一致：它按 strip().lower().startswith("dalton-")
    # 过滤、按 file_name 排序取第一条，这里用 strpos(lower(name), 'dalton-')
    # 等价筛选，并保持 ORDER BY file_name 的确定顺序。
    if user_id is not None:
        await cur.execute(
            """
            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
            FROM automations
            WHERE strpos(lower(name), 'dalton-') > 0 AND user_id = %s
            ORDER BY file_name
            """,
            (user_id,),
        )
    else:
        await cur.execute(
            """
            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
            FROM automations
            WHERE strpos(lower(name), 'dalton-') > 0
            ORDER BY file_name
            """
        )
    for ar in await cur.fetchall() or []:
        # The SQL only prefilters; tracking's own rule (Python strip(), which
        # also drops tabs and other whitespace btrim keeps) decides.
        if (
            isinstance(ar, dict)
            and ar.get("user_id")
            and str(ar.get("name") or "").strip().lower().startswith("dalton-")
        ):
            autos_by_user.setdefault(str(ar["user_id"]), []).append(dict(ar))
    return autos_by_user


async def load_colleague_workspace_access(
    user_id: str | None,
) -> dict[str, tuple[bool, bool]]:
    """Map each colleague-visible workspace to ``(web_readable, cos_rw)``.

    Reuses ``list_team_workspaces`` exactly as the board does: a workspace
    absent from the map is invisible (never listed), a present one carries
    its sharing decision. Hidden workspaces never appear, so a unit whose
    workspace is missing from this map must not surface either.
    """
    colleague_access: dict[str, tuple[bool, bool]] = {}
    if user_id is None:
        return colleague_access
    try:
        from src.dalton.sharing import list_team_workspaces

        for member in await list_team_workspaces(user_id):
            for cws in member.get("workspaces") or []:
                ws_id_c = str(cws.get("workspace_id") or "")
                if ws_id_c:
                    colleague_access[ws_id_c] = (
                        str(cws.get("web_access") or "hidden") != "hidden",
                        str(cws.get("cos_access") or "hidden") == "rw",
                    )
    except Exception as exc:
        logger.warning("[DALTON_UNITS_VIEW] team workspace lookup failed: %s", exc)
    return colleague_access


def member_workspace_visible(
    company_row: dict[str, Any] | None,
    user_id: str | None,
    colleague_access: dict[str, tuple[bool, bool]],
) -> bool:
    """A member's company workspace id is only output when the viewer may read it.

    Same owner, or present in ``list_team_workspaces``' visible result;
    otherwise only ``workspace_created`` and the name survive — member
    resolution never bypasses a colleague workspace's visibility filter.
    """
    if company_row is None:
        return False
    if user_id is None:
        return True
    if str(company_row.get("owner_user_id") or "") == str(user_id):
        return True
    return colleague_access.get(
        str(company_row.get("workspace_id") or ""), (False, False)
    )[0]


def render_sector_member(
    m: dict[str, Any],
    company_row: dict[str, Any] | None,
    visible: bool,
) -> dict[str, Any]:
    """One sector member row as the board and the detail endpoint output it."""
    return {
        "member_id": m.get("member_id"),
        "name": m.get("member_name"),
        "ticker": m.get("ticker"),
        "company_unit_id": m.get("company_unit_id"),
        "workspace_created": bool(m.get("workspace_created")),
        "workspace_id": (
            company_row.get("workspace_id") if visible else None
        ),
        "scope": m.get("scope") or "",
        "reason": m.get("reason") or "",
        "valid_until": (
            m["valid_until"].isoformat() if m.get("valid_until") else None
        ),
    }


async def _load_company_bindings(
    ucur: Any, member_rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Active company-unit bindings for the members of the given sectors.

    Only active bindings count: an archived company unit no longer points at
    its workspace, which would otherwise read as a false "workspace created".
    """
    company_unit_ids = sorted(
        {
            str(m.get("company_unit_id"))
            for m in member_rows
            if m.get("company_unit_id")
        }
    )
    company_ws_by_unit: dict[str, dict[str, Any]] = {}
    if company_unit_ids:
        await ucur.execute(
            """
            SELECT unit_id::text AS unit_id, status, owner_user_id,
                   workspace_id::text AS workspace_id
            FROM dalton_coverage_units
            WHERE unit_id = ANY(%s::uuid[])
            """,
            (company_unit_ids,),
        )
        for cr in await ucur.fetchall():
            company_ws_by_unit[str(cr["unit_id"])] = dict(cr)
        company_ws_by_unit = {
            uid: row
            for uid, row in company_ws_by_unit.items()
            if str(row.get("status") or "") == "active"
        }
    return company_ws_by_unit


async def build_units_view(
    user_id: str | None,
    autos_by_user: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The cross-owner coverage-unit list (P2 board ``units``).

    Own active units first; colleague units enter only through workspaces
    ``list_team_workspaces`` reports visible — a hidden workspace's unit
    never appears, so presence itself already means readable-or-delegatable.
    Raises on query failure; callers decide to degrade.
    """
    from src.dalton.database.sector_members import fetch_members_for_sectors

    colleague_access = await load_colleague_workspace_access(user_id)
    out: list[dict[str, Any]] = []
    async with get_db_connection() as uconn:
        async with uconn.cursor(row_factory=dict_row) as ucur:
            # 参数出现在 IS NULL 这种无类型上下文里时必须显式转型
            # （board codex r1 #1）：默认适配器发 unknown 类型，Postgres
            # 推断不出 $1，整个 units 视图会被吞成空。
            await ucur.execute(
                """
                SELECT unit_id::text AS unit_id, kind, owner_user_id, slug,
                       display_name, workspace_id::text AS workspace_id, ticker
                FROM dalton_coverage_units
                WHERE status = 'active'
                  AND (%s::text IS NULL OR owner_user_id = %s)
                ORDER BY kind, slug
                """,
                (user_id, user_id),
            )
            unit_rows = [dict(r) for r in await ucur.fetchall()]

            # 同事的单元只经由可见工作区进入（不泄漏隐藏工作区的存在），
            # 可见性判断全部复用 sharing 的既有结果。
            if colleague_access:
                await ucur.execute(
                    """
                    SELECT unit_id::text AS unit_id, kind, owner_user_id, slug,
                           display_name, workspace_id::text AS workspace_id, ticker
                    FROM dalton_coverage_units
                    WHERE status = 'active'
                      AND workspace_id = ANY(%s::uuid[])
                      AND (%s::text IS NULL OR owner_user_id <> %s)
                    ORDER BY kind, slug
                    """,
                    (list(colleague_access), user_id, user_id),
                )
                existing_ids = {u["unit_id"] for u in unit_rows}
                unit_rows.extend(
                    dict(r)
                    for r in await ucur.fetchall()
                    if str(r.get("unit_id")) not in existing_ids
                )

            sector_ids = [
                str(u["unit_id"]) for u in unit_rows if u.get("kind") == "sector"
            ]
            member_rows = await fetch_members_for_sectors(ucur, sector_ids)
            members_by_sector: dict[str, list[dict[str, Any]]] = {}
            for mr in member_rows:
                members_by_sector.setdefault(
                    str(mr.get("sector_unit_id")), []
                ).append(dict(mr))
            company_ws_by_unit = await _load_company_bindings(ucur, member_rows)

    own_auto_ws = {
        str(a.get("workspace_id") or "")
        for a in autos_by_user.get(str(user_id or ""), [])
        if a.get("workspace_id")
    }
    for u in unit_rows:
        u_id = str(u.get("unit_id"))
        owner = str(u.get("owner_user_id") or "")
        ws_id = str(u.get("workspace_id") or "") or None
        is_own = user_id is not None and owner == str(user_id)
        if is_own:
            readable = True
            # 未绑定工作区的单元没有可委派的对象。
            delegatable = ws_id is not None
            auto_tracked = bool(ws_id and ws_id in own_auto_ws)
        elif user_id is None:
            readable = True
            delegatable = False
            auto_tracked = False
        else:
            access = colleague_access.get(ws_id or "")
            if access is None:
                continue
            readable = access[0]
            delegatable = access[1]
            auto_tracked = False
        entry: dict[str, Any] = {
            "unit_id": u_id,
            "kind": u.get("kind"),
            "slug": u.get("slug"),
            "display_name": u.get("display_name"),
            "owner_user_id": owner,
            "workspace_id": ws_id,
            "readable": readable,
            "delegatable": delegatable,
            "auto_tracked": auto_tracked,
        }
        if u.get("kind") == "company":
            entry["ticker"] = u.get("ticker")
        else:
            members_out: list[dict[str, Any]] = []
            for m in members_by_sector.get(u_id, []):
                company_row = company_ws_by_unit.get(
                    str(m.get("company_unit_id") or "")
                )
                members_out.append(
                    render_sector_member(
                        m,
                        company_row,
                        member_workspace_visible(company_row, user_id, colleague_access),
                    )
                )
            entry["members"] = members_out
        out.append(entry)
    return out


async def list_unclassified_workspaces(user_id: str) -> list[dict[str, Any]]:
    """The caller's non-deleted workspaces bound to no active unit and not Home.

    These are the "未分类工作区" entry of the coverage view: workspaces the
    coverage map does not explain yet. Registration happens through the
    agent-side tools, never from this read path.
    """
    from src.server.database.home_workspace import is_home_row

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id, name,
                       description, updated_at
                FROM workspaces
                WHERE status <> 'deleted' AND user_id = %s
                ORDER BY name ASC
                """,
                (user_id,),
            )
            ws_rows = [dict(r) for r in await cur.fetchall()]
            units_by_ws = await load_active_units_by_workspace(
                [str(r["workspace_id"]) for r in ws_rows], conn=conn
            )
    out: list[dict[str, Any]] = []
    for ws in ws_rows:
        ws_id = str(ws["workspace_id"])
        if is_home_row(ws):
            continue
        if ws_id in units_by_ws:
            continue
        updated = ws.get("updated_at")
        out.append(
            {
                "workspace_id": ws_id,
                "name": ws.get("name"),
                "description": ws.get("description"),
                "updated_at": (
                    updated.isoformat()
                    if isinstance(updated, datetime)
                    else (str(updated) if updated else None)
                ),
            }
        )
    return out


def _summarize_thesis(thesis: dict[str, Any] | None) -> dict[str, Any] | None:
    """Thesis summary shared by the sector and company detail views."""
    if not thesis:
        return None
    content = thesis.get("content") or {}
    house_view = content.get("house_view") or {}
    pillars = [
        {
            "id": p.get("id"),
            "claim": p.get("claim"),
            "priority": p.get("priority"),
            "status": p.get("status"),
        }
        for p in (content.get("pillars") or [])
        if isinstance(p, dict)
    ]
    return {
        "version_no": thesis.get("version_no"),
        "one_liner": content.get("one_liner"),
        "aggregate_status": content.get("aggregate_status"),
        "stance": house_view.get("stance") or content.get("stance"),
        "conviction": house_view.get("conviction") or content.get("conviction"),
        "pillars": pillars,
    }


def _summarize_sector_docs(
    docs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Light, display-only summaries of a sector unit's knowledge documents."""
    sector = docs.get("sector")
    drivers = docs.get("drivers")
    agenda = docs.get("agenda")

    sector_out: dict[str, Any] | None = None
    if sector:
        content = sector.get("content") or {}
        scope = content.get("scope") or {}
        sector_out = {
            "version_no": sector.get("version_no"),
            "name": content.get("name"),
            "name_zh": content.get("name_zh"),
            "definition": content.get("definition"),
            "boundaries": content.get("boundaries"),
            "one_liner": content.get("one_liner"),
            "coverage_stage": content.get("coverage_stage"),
            "coverage_kind": content.get("kind"),
            "wiki_slug": content.get("wiki_slug"),
            "research_tz": content.get("research_tz"),
            "regions": scope.get("regions") or [],
            "markets": scope.get("markets") or [],
        }

    thesis_out = _summarize_thesis(docs.get("thesis"))

    drivers_out: dict[str, Any] | None = None
    if drivers:
        content = drivers.get("content") or {}

        def _indicator_out(ind: dict[str, Any]) -> dict[str, Any]:
            # drivers schema 的指标真实字段（codex p4 r1 #4）：指标名之外，
            # 最新读数（value/unit/period/as_of）才是页面要展示的内容；
            # readings 历史与 probe 是预检/滚动归档的运行时数据，不进摘要。
            latest = ind.get("latest")
            return {
                "id": ind.get("id"),
                "name": ind.get("name"),
                "frequency": ind.get("frequency"),
                "good_direction": ind.get("good_direction"),
                "latest": (
                    {
                        "value": latest.get("value"),
                        "unit": latest.get("unit"),
                        "period": latest.get("period"),
                        "as_of": latest.get("as_of"),
                    }
                    if isinstance(latest, dict)
                    else None
                ),
            }

        drivers_out = {
            "version_no": drivers.get("version_no"),
            "as_of": content.get("as_of"),
            "drivers": [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "kind": d.get("kind"),
                    "updated_at": d.get("updated_at"),
                    "leading_indicators": [
                        _indicator_out(ind)
                        for ind in (d.get("leading_indicators") or [])
                        if isinstance(ind, dict)
                    ],
                }
                for d in (content.get("drivers") or [])
                if isinstance(d, dict)
            ],
        }

    return {
        "sector": sector_out,
        "thesis": thesis_out,
        "drivers": drivers_out,
        "agenda_version_no": (agenda or {}).get("version_no"),
    }


def _extract_agenda_todos(agenda: dict[str, Any] | None) -> dict[str, Any]:
    """Open agenda items of one unit: open questions, due validations, events.

    Tolerant like the board's extraction — malformed entries are skipped, no
    scoring; this view answers "what is pending", the board ranks it.
    """
    if not agenda:
        return {"questions": [], "validations": [], "events": []}
    content = agenda.get("content") or {}

    questions: list[dict[str, Any]] = []
    for q in content.get("open_questions") or []:
        # 只排除真正已关闭的状态（codex p4 r1 #3）：waiting_pm 的问题还没
        # 解决，转交 PM 后仍要留在待办里并带等待标识。schema 状态枚举是
        # open/waiting_pm/resolved/dropped；investigating 是旧数据容忍。
        if not isinstance(q, dict) or q.get("status") in ("resolved", "dropped"):
            continue
        questions.append(
            {
                "id": str(q.get("id") or "Q-001"),
                "question": q.get("question") or q.get("title") or "",
                "why": q.get("why") or "",
                "value": str(q.get("value") or "medium").lower(),
                "urgency": str(q.get("urgency") or "this_week").lower(),
                "origin": q.get("origin"),
                "waiting_pm": bool(q.get("waiting_pm")) or q.get("status") == "waiting_pm",
            }
        )

    validations: list[dict[str, Any]] = []
    for v in (content.get("next_validations") or []) + (
        content.get("validation_queue") or []
    ):
        if not isinstance(v, dict) or v.get("status") not in (None, "open", "pending"):
            continue
        validations.append(
            {
                "id": str(v.get("id") or "V1"),
                "what": str(v.get("what") or v.get("metric") or v.get("title") or ""),
                "due": v.get("due") or v.get("target_date") or v.get("due_date"),
            }
        )

    events: list[dict[str, Any]] = []
    catalysts = ((content.get("watch") or {}).get("events") or []) + (
        content.get("catalysts") or []
    )
    for cat in catalysts:
        if isinstance(cat, dict) and cat.get("status") in (
            None, "upcoming", "watching", "active", "confirmed",
        ):
            events.append(
                {
                    "type": cat.get("type") or cat.get("kind") or "catalyst",
                    "title": cat.get("plan") or cat.get("title") or cat.get("event") or "",
                    "date": cat.get("date") or cat.get("expected_date"),
                    "date_status": cat.get("date_status") or "confirmed",
                }
            )

    return {"questions": questions, "validations": validations, "events": events}


def _extract_re_review_questions(agenda: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A company unit's open re-review items (P2 linkage, origin ``sector``)."""
    if not agenda:
        return []
    out: list[dict[str, Any]] = []
    for q in (agenda.get("content") or {}).get("open_questions") or []:
        if not isinstance(q, dict) or q.get("origin") != "sector":
            continue
        if q.get("status") not in (None, "open", "waiting_pm"):
            continue
        out.append(
            {
                "id": str(q.get("id") or "Q-001"),
                "question": q.get("question") or "",
                "value": str(q.get("value") or "medium").lower(),
                "urgency": str(q.get("urgency") or "this_week").lower(),
                "status": q.get("status") or "open",
                "source_ref": q.get("source_ref"),
            }
        )
    return out


async def _latest_valid_docs(
    cur: Any, workspace_id: str, doc_keys: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Latest schema-valid knowledge docs of one workspace, by doc key."""
    await cur.execute(
        """
        SELECT DISTINCT ON (doc_key) doc_key, version_no, content
        FROM dalton_knowledge_versions
        WHERE workspace_id = %s::uuid
          AND schema_valid = TRUE
          AND doc_key = ANY(%s::text[])
        ORDER BY doc_key, version_no DESC
        """,
        (workspace_id, list(doc_keys)),
    )
    return {
        str(r["doc_key"]): dict(r)
        for r in await cur.fetchall()
    }


async def build_units_listing(user_id: str) -> dict[str, Any]:
    """One-call coverage listing for the HTTP API: units + unclassified workspaces.

    A failed automations preload degrades to "no auto-tracked units" (same as
    the board) instead of failing the whole listing — the map stays readable
    even when the scheduler's table is briefly unavailable.
    """
    uid = str(user_id or "").strip()
    autos_by_user: dict[str, list[dict[str, Any]]] = {}
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                autos_by_user = await load_dalton_automations_by_user(cur, uid)
    except Exception as exc:
        logger.warning("[DALTON_UNITS_VIEW] automations preload failed: %s", exc)
    units = await build_units_view(uid, autos_by_user)
    unclassified = await list_unclassified_workspaces(uid)
    return {"enabled": True, "units": units, "unclassified": unclassified}


class UnitNotFound(LookupError):
    """The unit does not exist, is archived, or is invisible to the caller."""


async def build_unit_detail(user_id: str, unit_id: str) -> dict[str, Any]:
    """One coverage unit's read-only detail (P4 coverage view).

    Visibility follows the list view exactly: own units are readable, a
    colleague's unit exists only through a workspace ``list_team_workspaces``
    reports; anything else raises :class:`UnitNotFound` so the router can 404
    without leaking existence. The cross-unit ledger is owner-only by design
    (d026), so its sections appear only on the caller's own sector.
    """
    from src.dalton.database.sector_members import (
        fetch_members_for_sectors,
        list_members_for_company,
    )

    uid = str(user_id or "").strip()
    unit = await get_coverage_unit(unit_id)
    if unit is None or str(unit.get("status") or "") != "active":
        raise UnitNotFound(f"coverage unit {unit_id} not found")
    ws_id = str(unit.get("workspace_id") or "") or None
    owner = str(unit.get("owner_user_id") or "")
    is_own = owner == uid

    colleague_access = await load_colleague_workspace_access(uid)
    if is_own:
        readable = True
        delegatable = ws_id is not None
    else:
        access = colleague_access.get(ws_id or "")
        if access is None:
            raise UnitNotFound(f"coverage unit {unit_id} not found")
        readable = access[0]
        delegatable = access[1]

    auto_tracked = False
    if is_own and ws_id:
        try:
            async with get_db_connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    autos = await load_dalton_automations_by_user(cur, uid)
            auto_tracked = ws_id in {
                str(a.get("workspace_id") or "")
                for a in autos.get(uid, [])
                if a.get("workspace_id")
            }
        except Exception as exc:
            logger.warning("[DALTON_UNITS_VIEW] automations preload failed: %s", exc)

    entry: dict[str, Any] = {
        "unit_id": str(unit.get("unit_id")),
        "kind": unit.get("kind"),
        "slug": unit.get("slug"),
        "display_name": unit.get("display_name"),
        "owner_user_id": owner,
        "workspace_id": ws_id,
        "readable": readable,
        "delegatable": delegatable,
        "auto_tracked": auto_tracked,
    }
    if str(unit.get("kind")) == "company":
        entry["ticker"] = unit.get("ticker")

    docs: dict[str, dict[str, Any]] = {}
    if ws_id:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                docs = await _latest_valid_docs(
                    cur, ws_id, ("sector", "thesis", "drivers", "agenda", "company")
                )

    if str(unit.get("kind")) == "sector":
        members: list[dict[str, Any]] = []
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                member_rows = await fetch_members_for_sectors(cur, [entry["unit_id"]])
                company_ws_by_unit = await _load_company_bindings(cur, member_rows)
        for m in member_rows:
            company_row = company_ws_by_unit.get(str(m.get("company_unit_id") or ""))
            members.append(
                render_sector_member(
                    m,
                    company_row,
                    member_workspace_visible(company_row, uid, colleague_access),
                )
            )

        evidence_gaps: list[dict[str, Any]] = []
        ledger_gaps: list[dict[str, Any]] = []
        pending_total = 0
        if is_own:
            from src.dalton.database.cross_unit_requests import list_pending_for_sector

            # 先按行业和待处理状态过滤、再 LIMIT（codex p4 r1 #2）：读全局
            # 最新 200 条再在 Python 里筛，会把本行业的旧 gap 挤出窗口。
            # total 让前端能提示"仅显示前 N 条"。未归入证据缺口的 pending
            # 行都算 ledger 侧，保证 total 与展示条数可比。
            try:
                pending_rows, pending_total = await list_pending_for_sector(
                    uid, entry["unit_id"], ws_id, limit=50
                )
            except Exception as exc:
                logger.warning("[DALTON_UNITS_VIEW] ledger read failed: %s", exc)
                pending_rows = []
            for r in pending_rows:
                if (
                    str(r.get("kind") or "") == "research_request"
                    and str(r.get("status") or "") in ("open", "dispatched")
                ):
                    evidence_gaps.append(
                        {
                            "request_id": r.get("request_id"),
                            "question": r.get("question"),
                            "evidence_needed": r.get("evidence_needed") or [],
                            "completion_condition": r.get("completion_condition"),
                            "target_workspace_id": r.get("target_workspace_id"),
                            "status": r.get("status"),
                            "created_at": _iso_or_none(r.get("created_at")),
                        }
                    )
                else:
                    ledger_gaps.append(
                        {
                            "request_id": r.get("request_id"),
                            "kind": r.get("kind"),
                            "question": r.get("question"),
                            "target_workspace_id": r.get("target_workspace_id"),
                            "status": r.get("status"),
                            "created_at": _iso_or_none(r.get("created_at")),
                            "updated_at": _iso_or_none(r.get("updated_at")),
                        }
                    )

        entry["sector"] = {
            "docs": _summarize_sector_docs(docs) if ws_id else None,
            "agenda_todos": (
                _extract_agenda_todos(docs.get("agenda")) if ws_id else None
            ),
            "members": members,
            "evidence_gaps": evidence_gaps,
            "ledger_gaps": ledger_gaps,
            "pending_total": pending_total,
        }
        return entry

    # company unit: related sectors (reverse lookup) + pending re-review items
    sectors_out: list[dict[str, Any]] = []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            member_rows = await list_members_for_company(entry["unit_id"])
            sector_unit_ids = sorted(
                {
                    str(m.get("sector_unit_id"))
                    for m in member_rows
                    if m.get("sector_unit_id")
                }
            )
            sector_units: dict[str, dict[str, Any]] = {}
            if sector_unit_ids:
                await cur.execute(
                    """
                    SELECT unit_id::text AS unit_id, kind, owner_user_id, slug,
                           display_name, workspace_id::text AS workspace_id, status
                    FROM dalton_coverage_units
                    WHERE unit_id = ANY(%s::uuid[])
                    """,
                    (sector_unit_ids,),
                )
                sector_units = {
                    str(r["unit_id"]): dict(r) for r in await cur.fetchall()
                }

    for m in member_rows:
        s_unit = sector_units.get(str(m.get("sector_unit_id") or ""))
        if s_unit is None or str(s_unit.get("status") or "") != "active":
            continue
        s_owner = str(s_unit.get("owner_user_id") or "")
        s_ws = str(s_unit.get("workspace_id") or "") or None
        if s_owner == uid:
            s_readable, s_delegatable = True, s_ws is not None
        else:
            s_access = colleague_access.get(s_ws or "")
            if s_access is None:
                # 同事的隐藏工作区不进入关联列表（与列表视图同一规则）
                continue
            s_readable, s_delegatable = s_access
        sectors_out.append(
            {
                "unit_id": str(s_unit.get("unit_id")),
                "slug": s_unit.get("slug"),
                "display_name": s_unit.get("display_name"),
                "member_id": m.get("member_id"),
                "scope": m.get("scope") or "",
                "valid_until": (
                    m["valid_until"].isoformat() if m.get("valid_until") else None
                ),
                "readable": s_readable,
                "delegatable": s_delegatable,
                "workspace_id": s_ws if s_readable else None,
            }
        )

    entry["company"] = {
        "docs": ({"thesis": _summarize_thesis(docs.get("thesis"))} if ws_id else None),
        "sectors": sectors_out,
        "re_review": (
            _extract_re_review_questions(docs.get("agenda")) if ws_id else []
        ),
    }
    return entry


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None
