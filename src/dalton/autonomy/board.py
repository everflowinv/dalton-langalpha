"""Coverage board aggregator for the Dalton Research Director and UI (PK.19)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from src.dalton.autonomy.due import ticker_market_timezone, validation_is_due
from src.dalton.budget.service import get_budget_today
from src.dalton.coverage import get_user_coverage
from src.dalton.coverage.identity import resolve_workspace_coverage_identity
from src.dalton.database.coverage_units import load_active_units_by_workspace
from src.dalton.settings import get_dalton_settings, is_coverage_units_enabled
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_EST_COST_BY_KIND: dict[str, float] = {
    "earnings_preview": 3.5,
    "earnings_flash": 1.5,
    "earnings_deep": 5.0,
    "event_note": 1.2,
    "validation_check": 1.8,
    "open_question": 2.0,
    "topic": 6.0,
}

VALUE_WEIGHT = {"high": 3.0, "P0": 3.0, "medium": 2.0, "P1": 2.0, "low": 1.0, "P2": 1.0}
URGENCY_WEIGHT = {
    "overdue": 3.0,
    "now": 3.0,
    "today": 2.5,
    "tm3": 2.5,
    "this_week": 2.0,
    "this_quarter": 1.0,
    "someday": 0.5,
    "later": 0.5,
}


async def _load_user_coverage_map(user_id: str | None) -> dict[str, dict[str, Any]]:
    try:
        raw = await get_user_coverage(user_id)
        entries = raw.get("companies") or raw.get("coverage") or []
        out: dict[str, dict[str, Any]] = {}
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and item.get("ticker"):
                    out[str(item["ticker"]).upper()] = item
        return out
    except Exception as e:
        logger.warning("[DALTON_BOARD] failed to load user coverage for %s: %s", user_id, e)
        return {}


def _compute_staleness(last_research_iso: str | None, today: date) -> float:
    if not last_research_iso:
        return 1.5
    try:
        d = date.fromisoformat(str(last_research_iso)[:10])
        days = max(0, (today - d).days)
        return min(2.0, 1.0 + (days / 14.0))
    except ValueError:
        return 1.2


def _is_backoff_active(backoff_until: Any, now_dt: datetime, today: date) -> bool:
    if not backoff_until:
        return False
    raw = str(backoff_until).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now_dt.tzinfo or timezone.utc)
        return dt > now_dt
    except ValueError:
        try:
            return date.fromisoformat(raw[:10]) > today
        except ValueError:
            return False


async def build_coverage_board(
    user_id: str | None = None,
    tier: str = "all",
    stale_only: bool = False,
) -> dict[str, Any]:
    """Assemble the cross-company coverage board and candidate ranking (PK.19)."""
    settings = get_dalton_settings()
    tz_name = settings.autonomy.timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now_dt = datetime.now(tz)
    today = now_dt.date()
    cov_map = await _load_user_coverage_map(user_id)

    budget_raw: dict[str, Any] = {}
    try:
        budget_raw = await get_budget_today(user_id=user_id)
    except Exception as e:
        logger.warning("[DALTON_BOARD] get_budget_today failed: %s", e)

    cap_usd = float(
        budget_raw["cap_usd"]
        if "cap_usd" in budget_raw and budget_raw["cap_usd"] is not None
        else (budget_raw.get("daily_cap_usd") or 0.0)
    )
    spent_usd = float(
        budget_raw["spent_usd"]
        if "spent_usd" in budget_raw and budget_raw["spent_usd"] is not None
        else (budget_raw.get("spent_today_usd") or 0.0)
    )
    reserved_usd = float(
        budget_raw["open_reserved_usd"]
        if "open_reserved_usd" in budget_raw and budget_raw["open_reserved_usd"] is not None
        else (budget_raw.get("reserved_usd") or 0.0)
    )
    remaining_usd = max(0.0, round(cap_usd - spent_usd - reserved_usd, 2)) if cap_usd > 0 else 50.0
    reserve_for_events_usd = round(min(remaining_usd * 0.2, 6.0), 2)
    allocatable_usd = max(0.0, round(remaining_usd - reserve_for_events_usd, 2))

    budget_summary = {
        **budget_raw,
        "cap_usd": cap_usd,
        "spent_usd": spent_usd,
        "open_reserved_usd": reserved_usd,
        "remaining_usd": remaining_usd,
        "reserve_for_events_usd": reserve_for_events_usd,
        "allocatable_usd": allocatable_usd,
    }

    companies: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    pm_handoffs: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []
    # 行业层 P2：开关开启时看板额外给出 units（覆盖单元视图）；关闭时保持
    # None——输出键、查询路径与 upstream 完全一致。
    units: list[dict[str, Any]] | None = None
    if is_coverage_units_enabled():
        units = []

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if user_id is not None:
                    await cur.execute(
                        """
                        SELECT workspace_id, user_id, name, description, status, updated_at
                        FROM workspaces
                        WHERE status <> 'deleted' AND user_id = %s
                        ORDER BY name ASC
                        """,
                        (user_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT workspace_id, user_id, name, description, status, updated_at
                        FROM workspaces
                        WHERE status <> 'deleted'
                        ORDER BY name ASC
                        """
                    )
                ws_rows = await cur.fetchall()
                user_ws_ids = {str(r["workspace_id"]) for r in ws_rows}

                # 行业层 P0：开关打开时一次性取每个工作区绑定的 active 单元；
                # 关闭时不发这条查询。
                units_by_ws: dict[str, dict[str, Any]] = {}
                if ws_rows and is_coverage_units_enabled():
                    try:
                        units_by_ws = await load_active_units_by_workspace(
                            [str(r["workspace_id"]) for r in ws_rows], conn=conn
                        )
                    except Exception as exc:
                        logger.warning(
                            "[DALTON_BOARD] coverage unit lookup failed: %s", exc
                        )
                        units_by_ws = {}

                # 行业层 P0 round 2（codex #4）：兼容回退还需要该用户已有的
                # dalton-* 自动化行——没有文档、只有自动化的老公司工作区在开关
                # 关闭时也要照常出现在看板上。与 tracking 的回退同一条规则：
                # 不过滤状态（暂停也是已验证的跟踪信息）。
                autos_by_user: dict[str, list[dict[str, Any]]] = {}
                try:
                    # 与 tracking 的筛选和顺序一致（codex r2 #1/#2）：它按
                    # strip().lower().startswith("dalton-") 过滤、按 file_name
                    # 排序取第一条，这里用 lower(btrim(name)) LIKE 等价筛选，
                    # 并保持 ORDER BY file_name 的确定顺序。
                    if user_id is not None and ws_rows:
                        await cur.execute(
                            """
                            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
                            FROM automations
                            WHERE strpos(lower(name), 'dalton-') > 0 AND user_id = %s
                            ORDER BY file_name
                            """,
                            (user_id,),
                        )
                    elif ws_rows:
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
                except Exception as exc:
                    logger.warning("[DALTON_BOARD] dalton automation preload failed: %s", exc)
                    autos_by_user = {}

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id, doc_key)
                           workspace_id, doc_key, version_no, content, created_at
                    FROM dalton_knowledge_versions
                    WHERE schema_valid = TRUE
                      AND doc_key IN ('thesis', 'agenda', 'company')
                    ORDER BY workspace_id, doc_key, version_no DESC
                    """
                )
                kv_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id)
                           workspace_id, verdict_id, verdict_at, word,
                           trigger_kind, trigger_ref, progress, next_due, payload,
                           conversation_thread_id, created_at
                    FROM dalton_verdicts
                    ORDER BY workspace_id, verdict_at DESC, length(verdict_id) DESC, verdict_id DESC
                    """
                )
                verdict_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id)
                           publication_id, workspace_id, ticker, kind, title,
                           published_date, thesis_version_no, created_at
                    FROM dalton_publications
                    ORDER BY workspace_id, published_date DESC, created_at DESC
                    """
                )
                pub_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT publish_request_id, workspace_id, conversation_thread_id,
                           kind, title, slug, draft_path, review, created_at
                    FROM dalton_publish_requests
                    WHERE status = 'awaiting_pm'
                    ORDER BY created_at DESC
                    """
                )
                req_rows = await cur.fetchall()
                if user_id is not None:
                    req_rows = [r for r in req_rows if str(r.get("workspace_id") or "") in user_ws_ids]
                pending_approvals = [
                    {
                        "request_id": str(r["publish_request_id"]),
                        "publish_request_id": str(r["publish_request_id"]),
                        "workspace_id": str(r["workspace_id"]),
                        "thread_id": str(r["conversation_thread_id"]),
                        "kind": r.get("kind"),
                        "title": r.get("title"),
                        "draft_path": r.get("draft_path"),
                        "review": r.get("review"),
                        "created_at": str(r.get("created_at") or ""),
                    }
                    for r in req_rows
                ]

                await cur.execute(
                    """
                    SELECT signal_id, workspace_id, ticker, probe, signal_key, priority, summary, detected_at
                    FROM dalton_signals
                    WHERE resolved_at IS NULL
                      AND expired_at IS NULL
                    ORDER BY detected_at DESC
                    """
                )
                sig_rows = await cur.fetchall()
                if user_id is not None:
                    sig_rows = [s for s in sig_rows if str(s.get("workspace_id") or "") in user_ws_ids]

                watch_state_by_ws: dict[str, dict[str, Any]] = {}
                try:
                    await cur.execute(
                        """
                        SELECT workspace_id, state
                        FROM dalton_watch_state
                        """
                    )
                    for wr in await cur.fetchall() or []:
                        if isinstance(wr, dict) and wr.get("workspace_id") and isinstance(wr.get("state"), dict):
                            watch_state_by_ws[str(wr["workspace_id"])] = dict(wr["state"])
                except Exception:
                    watch_state_by_ws = {}

        kv_by_ws: dict[str, dict[str, dict[str, Any]]] = {}
        for r in kv_rows:
            ws_id = str(r["workspace_id"])
            kv_by_ws.setdefault(ws_id, {})[str(r["doc_key"])] = dict(r)

        verdict_by_ws = {str(r["workspace_id"]): dict(r) for r in verdict_rows}
        pub_by_ws = {str(r["workspace_id"]): dict(r) for r in pub_rows}
        sigs_by_ws: dict[str, list[dict[str, Any]]] = {}
        for s in sig_rows:
            if s.get("workspace_id"):
                sigs_by_ws.setdefault(str(s["workspace_id"]), []).append(dict(s))

        reqs_by_ws: dict[str, list[dict[str, Any]]] = {}
        for req in pending_approvals:
            if req.get("workspace_id"):
                reqs_by_ws.setdefault(str(req["workspace_id"]), []).append(req)

        for ws in ws_rows:
            ws_id = str(ws["workspace_id"])
            docs = kv_by_ws.get(ws_id) or {}
            thesis_content = (docs.get("thesis") or {}).get("content") or {}
            agenda_content = (docs.get("agenda") or {}).get("content") or {}
            company_content = (docs.get("company") or {}).get("content") or {}

            # 行业层 P0 round 2：身份与 ticker 一律来自共享解析器（codex #2/#3）。
            # Home、已登记行业单元无条件不进公司看板（名称再像 ticker 也不行）；
            # ticker 与交易所时区都用解析结果，不再从文档或名称重取。
            identity = await resolve_workspace_coverage_identity(
                ws,
                company_content=(
                    (docs.get("company") or {}).get("content") if "company" in docs else None
                ),
                thesis_content=(
                    (docs.get("thesis") or {}).get("content") if "thesis" in docs else None
                ),
                registered_units=units_by_ws,
                automations=autos_by_user.get(str(ws.get("user_id") or ""), []),
            )
            if not identity.is_company or not identity.ticker:
                continue
            ticker = identity.ticker
            market_tz = ticker_market_timezone(ticker, company_content)

            cov_meta = cov_map.get(ticker) or {}
            house_view = thesis_content.get("house_view") or {}
            one_liner = (
                house_view.get("one_liner")
                or thesis_content.get("one_liner")
                or company_content.get("one_liner")
                or ws.get("description")
                or ""
            )
            conviction = house_view.get("conviction") or thesis_content.get("conviction") or "medium"
            stance = house_view.get("stance") or thesis_content.get("stance") or "neutral"

            last_verdict = verdict_by_ws.get(ws_id)
            last_pub = pub_by_ws.get(ws_id)
            last_research_at = (
                last_verdict["verdict_at"].isoformat()[:10]
                if last_verdict and hasattr(last_verdict.get("verdict_at"), "isoformat")
                else (
                    str(last_pub.get("published_date") or "")[:10]
                    if last_pub and last_pub.get("published_date")
                    else None
                )
            )
            staleness = _compute_staleness(last_research_at, today)

            open_questions_list: list[dict[str, Any]] = []
            for q in agenda_content.get("open_questions") or []:
                if not isinstance(q, dict) or q.get("status") not in (None, "open", "investigating"):
                    continue
                qid = str(q.get("id") or "Q-001")
                val = str(q.get("value") or q.get("priority") or "medium").lower()
                urg = str(q.get("urgency") or "this_week").lower()
                backoff_until = q.get("next_attempt_after") or q.get("backoff_until")
                in_backoff = _is_backoff_active(backoff_until, now_dt, today)
                waiting_pm = bool(q.get("waiting_pm")) or q.get("status") == "waiting_pm"
                open_questions_list.append(
                    {
                        "id": qid,
                        "question": q.get("question") or q.get("title") or qid,
                        "value": val,
                        "urgency": urg,
                        "backoff_until": backoff_until,
                        "waiting_pm": waiting_pm,
                    }
                )
                if not waiting_pm and not in_backoff:
                    v_w = VALUE_WEIGHT.get(val, 2.0)
                    u_w = URGENCY_WEIGHT.get(urg, 1.5)
                    score = round(v_w * u_w * staleness, 2)
                    est_usd = DEFAULT_EST_COST_BY_KIND["open_question"]
                    raw_candidates.append(
                        {
                            "workspace_id": ws_id,
                            "ticker": ticker,
                            "task": f"跟进问题 {qid}：{q.get('question') or q.get('title') or qid}",
                            "kind": "open_question",
                            "score": score,
                            "est_usd": est_usd,
                            "why": f"价值={val} · 紧迫度={urg} · 距上次研究权重={staleness:.2f}",
                        }
                    )

            for v in (agenda_content.get("next_validations") or []) + (
                agenda_content.get("validation_queue") or []
            ):
                if not isinstance(v, dict) or v.get("status") not in (None, "open", "pending"):
                    continue
                vid = str(v.get("id") or "V1")
                v_what = str(v.get("what") or v.get("metric") or v.get("title") or vid)
                due_raw = v.get("due") or v.get("target_date") or v.get("due_date")
                if not due_raw:
                    continue
                # 与议程探针同一到期口径：保留原始时刻/时区，缺时区按标的交易所
                # 时区，不按本地（上海）日历日提前判到期。
                if validation_is_due(due_raw, ticker, now_dt, tz=market_tz):
                    score = round(3.0 * 3.0 * staleness, 2)
                    raw_candidates.append(
                        {
                            "workspace_id": ws_id,
                            "ticker": ticker,
                            "task": f"验证节点到期 {vid}：{v_what}",
                            "kind": "validation_check",
                            "score": score,
                            "est_usd": DEFAULT_EST_COST_BY_KIND["validation_check"],
                            "why": f"验证节点到期（{str(due_raw)[:10]}）· 距上次研究权重={staleness:.2f}",
                        }
                    )

            catalysts = ((agenda_content.get("watch") or {}).get("events") or []) + (
                agenda_content.get("catalysts") or []
            )
            next_events: list[dict[str, Any]] = []
            for cat in catalysts:
                if isinstance(cat, dict) and cat.get("status") in (None, "upcoming", "watching", "active", "confirmed"):
                    date_status = str(cat.get("date_status") or "confirmed")
                    next_events.append(
                        {
                            "type": cat.get("type") or cat.get("kind") or "catalyst",
                            "title": cat.get("plan") or cat.get("title") or cat.get("event") or "催化剂",
                            "date": cat.get("date") or cat.get("expected_date"),
                            "date_status": "待确认" if date_status == "estimated" else date_status,
                        }
                    )

            waiting_pm_list: list[dict[str, Any]] = []
            for ho in agenda_content.get("pm_handoffs") or []:
                if isinstance(ho, dict) and ho.get("status") in (None, "open", "pending", "waiting"):
                    item = {
                        "id": ho.get("id") or "H1",
                        "what": ho.get("summary") or ho.get("what") or ho.get("question") or "",
                        "since": ho.get("created_at") or ho.get("since") or "",
                    }
                    waiting_pm_list.append(item)
                    pm_handoffs.append({"ticker": ticker, "workspace_id": ws_id, **item})

            ws_sigs = sigs_by_ws.get(ws_id) or []
            for s in ws_sigs[:3]:
                prio = str(s.get("priority") or "P1")
                v_w = VALUE_WEIGHT.get(prio, 2.0)
                score = round(v_w * 2.5 * staleness, 2)
                raw_candidates.append(
                    {
                        "workspace_id": ws_id,
                        "ticker": ticker,
                        "task": f"处理未消费信号 [{s.get('probe')}]：{s.get('summary')}",
                        "kind": "event_note",
                        "score": score,
                        "est_usd": DEFAULT_EST_COST_BY_KIND["event_note"],
                        "why": f"未消费信号（{prio}）",
                    }
                )

            v_payload = (last_verdict or {}).get("payload") or {}
            companies.append(
                {
                    "workspace_id": ws_id,
                    "ticker": ticker,
                    "company_name": company_content.get("name") or cov_meta.get("name") or ticker,
                    "tier": str(cov_meta.get("tier") or company_content.get("tier") or "core").lower(),
                    "sector": cov_meta.get("sector") or company_content.get("sector") or "",
                    "aggregate_status": thesis_content.get("aggregate_status") or "intact",
                    "house_view": {"stance": stance, "conviction": conviction},
                    "stance": stance,
                    "conviction": conviction,
                    "one_liner": one_liner,
                    "thesis_version": (docs.get("thesis") or {}).get("version_no"),
                    "official_version": house_view.get("official_version"),
                    "last_verdict": (
                        {
                            "id": last_verdict.get("verdict_id"),
                            "verdict_id": last_verdict.get("verdict_id"),
                            "word": last_verdict.get("word"),
                            "verdict": last_verdict.get("word"),
                            "at": (
                                last_verdict["verdict_at"].isoformat()
                                if hasattr(last_verdict.get("verdict_at"), "isoformat")
                                else str(last_verdict.get("verdict_at") or "")
                            ),
                            "event_date": (
                                last_verdict["verdict_at"].isoformat()[:10]
                                if hasattr(last_verdict.get("verdict_at"), "isoformat")
                                else str(last_verdict.get("verdict_at") or "")[:10]
                            ),
                            "summary": v_payload.get("summary") or v_payload.get("rationale") or "",
                            "thread_id": (
                                str(last_verdict["conversation_thread_id"])
                                if last_verdict.get("conversation_thread_id")
                                else None
                            ),
                        }
                        if last_verdict
                        else None
                    ),
                    "open_questions": open_questions_list,
                    "open_signals": [
                        {
                            "id": str(s.get("signal_id")),
                            "kind": s.get("probe"),
                            "summary": s.get("summary"),
                        }
                        for s in ws_sigs
                    ],
                    "next_events": next_events,
                    "next_catalyst": next_events[0] if next_events else None,
                    "waiting_pm": waiting_pm_list,
                    "publish_awaiting_pm": [
                        {
                            "request_id": str(r.get("request_id")),
                            "title": r.get("title"),
                        }
                        for r in (reqs_by_ws.get(ws_id) or [])
                    ],
                    "last_publication": (
                        {
                            "publication_id": str(last_pub.get("publication_id")),
                            "title": last_pub.get("title"),
                            "report_type": last_pub.get("kind"),
                            "published_at": str(last_pub.get("published_date") or ""),
                        }
                        if last_pub
                        else None
                    ),
                    "last_research_at": last_research_at,
                    "pending_signals": len(ws_sigs),
                    "sec_probe_degraded": bool(
                        (watch_state_by_ws.get(ws_id) or {}).get("sec_probe_degraded")
                    ),
                }
            )

        # 行业层 P2：units 覆盖单元视图。开关关闭时本块整体不执行——
        # 不查 dalton_coverage_units / dalton_sector_members，输出不带
        # units 键。上方主查询的连接此时已归还池里，这里必须重新租借一
        # 个连接上下文（codex r1 #2）；同事可见工作区复用 sharing 的现成
        # 枚举（隐藏的不出现，出现即代表可读）。
        if units is not None:
            try:
                from src.dalton.database.sector_members import fetch_members_for_sectors

                colleague_access: dict[str, tuple[bool, bool]] = {}
                async with get_db_connection() as uconn:
                    async with uconn.cursor(row_factory=dict_row) as ucur:
                        # 参数出现在 IS NULL 这种无类型上下文里时必须显式
                        # 转型（codex r1 #1）：默认适配器发 unknown 类型，
                        # Postgres 推断不出 $1，整个 units 视图会被吞成空。
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

                        # 同事的单元只经由可见工作区进入（不泄漏隐藏工作区
                        # 的存在），可见性判断全部复用 sharing 的既有结果。
                        if user_id is not None:
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
                                logger.warning(
                                    "[DALTON_BOARD] team workspace lookup failed: %s", exc
                                )
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
                            # 只认 active 绑定：归档的公司单元不再指向工作区，
                            # 避免"已建工作区"的假阳性。
                            company_ws_by_unit = {
                                uid: row
                                for uid, row in company_ws_by_unit.items()
                                if str(row.get("status") or "") == "active"
                            }

                def _member_workspace_visible(company_row: dict[str, Any] | None) -> bool:
                    """成员的公司工作区 id 只有查看者有权读取时才输出（codex r1 #5）。

                    同一所有者，或在 list_team_workspaces 的可见结果里；无权时
                    只保留 workspace_created 与名称，不输出 id——成员解析不允
                    许绕过同事工作区的可见性过滤。
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
                                {
                                    "member_id": m.get("member_id"),
                                    "name": m.get("member_name"),
                                    "ticker": m.get("ticker"),
                                    "company_unit_id": m.get("company_unit_id"),
                                    "workspace_created": bool(m.get("workspace_created")),
                                    "workspace_id": (
                                        company_row.get("workspace_id")
                                        if _member_workspace_visible(company_row)
                                        else None
                                    ),
                                    "scope": m.get("scope") or "",
                                    "reason": m.get("reason") or "",
                                    "valid_until": (
                                        m["valid_until"].isoformat()
                                        if m.get("valid_until")
                                        else None
                                    ),
                                }
                            )
                        entry["members"] = members_out
                    units.append(entry)
            except Exception as exc:
                logger.warning("[DALTON_BOARD] units view build failed: %s", exc)
    except Exception as e:
        logger.warning("[DALTON_BOARD] DB query fallback: %s", e)

    raw_candidates.sort(key=lambda c: (-float(c["score"]), str(c["ticker"])))
    selected_candidates: list[dict[str, Any]] = []
    running_est = 0.0
    for item in raw_candidates:
        est = float(item.get("est_usd") or 1.5)
        if cap_usd <= 0 or (running_est + est <= allocatable_usd + 1e-6):
            running_est = round(running_est + est, 2)
            selected_candidates.append(
                {
                    "rank": len(selected_candidates) + 1,
                    **item,
                }
            )

    board_out: dict[str, Any] = {
        "date": today.isoformat(),
        "tz": tz_name,
        "generated_at": now_dt.isoformat(),
        "budget": budget_summary,
        "companies": companies,
        "candidates": selected_candidates,
        "est_cost_by_kind": DEFAULT_EST_COST_BY_KIND,
        "pending_approvals": pending_approvals,
        "pm_handoffs": pm_handoffs,
    }
    # 行业层 P2：开关关闭时键缺席，输出与 upstream 逐字一致；开启时补充
    # units（companies 保持原样不动）。
    if units is not None:
        board_out["units"] = units
    return board_out
