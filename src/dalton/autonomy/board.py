"""Coverage board aggregator for the Dalton Research Director and UI (PK.19)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from src.dalton.autonomy.due import ticker_market_timezone, validation_is_due
from src.dalton.autonomy.units_view import (
    build_units_view,
    load_dalton_automations_by_user,
)
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
REVALUATION_VERDICT_WORDS = frozenset(
    {"NEW_THESIS", "THESIS_STRENGTHENED", "THESIS_WEAKENED", "THESIS_BROKEN"}
)
_NO_DECISION_MARKERS = frozenset(
    {
        "none",
        "n/a",
        "na",
        "null",
        "无",
        "无。",
        "暂无",
        "不改变",
        "无影响",
        "无决定",
        "-",
    }
)
_DECISION_TRAILING_PUNCT = " \t\r\n.。!！?？;；,，、:：~～…"


def normalize_decision_impact(val: Any) -> str:
    """Return stripped decision-impact string, or ``""`` if empty or a placeholder marker."""
    if val is None:
        return ""
    raw = str(val).strip()
    if not raw:
        return ""
    cleaned = raw.rstrip(_DECISION_TRAILING_PUNCT).strip().lower()
    if not cleaned or cleaned in _NO_DECISION_MARKERS or raw.lower() in _NO_DECISION_MARKERS:
        return ""
    return raw


def resolve_decision_impact(item: dict[str, Any]) -> str:
    """Resolve ``decision_impact`` from a dict, falling back to ``why`` when ``decision_impact`` is empty or ``null``."""
    raw_di = item.get("decision_impact")
    if raw_di is not None:
        s_di = str(raw_di).strip()
        if s_di and s_di.rstrip(_DECISION_TRAILING_PUNCT).strip().lower() != "null":
            return normalize_decision_impact(raw_di)
    return normalize_decision_impact(item.get("why"))


def _parse_utc_epoch(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc).timestamp()
    raw = str(val).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            d = date.fromisoformat(raw[:10])
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


def _to_iso_timestamp(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    raw = str(val).strip()
    if not raw:
        return None
    if _parse_utc_epoch(raw) is not None:
        return raw
    return None


def _latest_iso_timestamp(*timestamps: str | None) -> str | None:
    best_str: str | None = None
    best_key: tuple[float, str] | None = None
    for ts in timestamps:
        if not ts:
            continue
        epoch = _parse_utc_epoch(ts)
        if epoch is None:
            continue
        key = (epoch, ts)
        if best_key is None or key > best_key:
            best_key = key
            best_str = ts
    return best_str


def _verdict_sort_key(row: dict[str, Any]) -> tuple[float, int, str]:
    raw_ts = (
        row.get("verdict_at")
        if row.get("verdict_at") is not None
        else row.get("created_at")
    )
    epoch = _parse_utc_epoch(raw_ts)
    vid = str(row.get("verdict_id") or "")
    return (epoch if epoch is not None else -1.0, len(vid), vid)


def _days_since(iso_date: str | None, today: date) -> int | None:
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(str(iso_date)[:10])
        return max(0, (today - d).days)
    except ValueError:
        return None


def _extract_scenario_version_increment_at(
    thesis_rows: list[dict[str, Any]],
    latest_thesis_content: dict[str, Any] | None = None,
) -> str | None:
    """Return the latest timestamp when ``thesis.underwriting.scenario_version`` incremented.

    Only an increase over the previous version's ``scenario_version`` counts;
    the first historical record never counts regardless of its ``scenario_version`` value.
    A ``thesis`` without ``underwriting`` returns ``None`` so ``last_revaluation_at``
    relies solely on non-``NO_CHANGE`` five-word verdicts.
    """
    if not thesis_rows:
        return None
    ordered = sorted(thesis_rows, key=lambda r: int(r.get("version_no") or 0))
    eff_latest = latest_thesis_content
    if eff_latest is None:
        last_c = ordered[-1].get("content")
        if isinstance(last_c, dict):
            eff_latest = last_c
    if isinstance(eff_latest, dict):
        latest_uw = eff_latest.get("underwriting")
        if not isinstance(latest_uw, dict) or not latest_uw:
            return None

    prev_sv: int | None = None
    last_inc_at: str | None = None
    for row in ordered:
        sv = row.get("scenario_version")
        prev_from_sql = row.get("prev_scenario_version")
        if not isinstance(sv, int) or isinstance(sv, bool):
            content = row.get("content")
            uw = content.get("underwriting") if isinstance(content, dict) else None
            sv = uw.get("scenario_version") if isinstance(uw, dict) else None
        if not isinstance(sv, int) or isinstance(sv, bool):
            continue
        row_ts = _to_iso_timestamp(row.get("created_at"))
        if isinstance(prev_from_sql, int) and not isinstance(prev_from_sql, bool):
            if sv > prev_from_sql and row_ts:
                last_inc_at = row_ts
            prev_sv = sv
            continue
        if prev_sv is not None and sv > prev_sv and row_ts:
            last_inc_at = row_ts
        prev_sv = sv
    return last_inc_at


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
                # 不过滤状态（暂停也是已验证的跟踪信息）。查询只在确有工作区
                # 时发出（与抽出前的条件一致）。
                autos_by_user: dict[str, list[dict[str, Any]]] = {}
                try:
                    if ws_rows:
                        autos_by_user = await load_dalton_automations_by_user(
                            cur, user_id
                        )
                except Exception as exc:
                    logger.warning(
                        "[DALTON_BOARD] dalton automation preload failed: %s", exc
                    )
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

                sv_rows: list[dict[str, Any]] = []
                if ws_rows:
                    await cur.execute(
                        """
                        SELECT DISTINCT ON (workspace_id)
                               workspace_id, version_no, scenario_version, prev_scenario_version, created_at
                        FROM (
                            SELECT workspace_id,
                                   version_no,
                                   (content->'underwriting'->>'scenario_version')::bigint AS scenario_version,
                                   LAG((content->'underwriting'->>'scenario_version')::bigint) OVER (
                                       PARTITION BY workspace_id ORDER BY version_no ASC
                                   ) AS prev_scenario_version,
                                   created_at
                            FROM dalton_knowledge_versions
                            WHERE workspace_id = ANY(%s)
                              AND schema_valid = TRUE
                              AND doc_key = 'thesis'
                              AND (content->'underwriting'->>'scenario_version') ~ '^[0-9]+$'
                        ) sv_history
                        WHERE prev_scenario_version IS NOT NULL
                          AND scenario_version > prev_scenario_version
                        ORDER BY workspace_id, version_no DESC
                        """,
                        ([str(r["workspace_id"]) for r in ws_rows],),
                    )
                    sv_rows = await cur.fetchall()

                await cur.execute(
                    """
                    SELECT DISTINCT ON (workspace_id, (word = 'NO_CHANGE'))
                           workspace_id, verdict_id, verdict_at, word,
                           trigger_kind, trigger_ref, progress, next_due, payload,
                           conversation_thread_id, created_at
                    FROM dalton_verdicts
                    ORDER BY workspace_id, (word = 'NO_CHANGE'), verdict_at DESC, length(verdict_id) DESC, verdict_id DESC
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
            doc_key = str(r["doc_key"])
            row_dict = dict(r)
            ws_docs = kv_by_ws.setdefault(ws_id, {})
            if doc_key not in ws_docs or int(row_dict.get("version_no") or 0) > int(
                ws_docs[doc_key].get("version_no") or 0
            ):
                ws_docs[doc_key] = row_dict

        thesis_rows_by_ws: dict[str, list[dict[str, Any]]] = {}
        for r in sv_rows or []:
            if not isinstance(r, dict) or not r.get("workspace_id"):
                continue
            if r.get("doc_key") not in (None, "thesis"):
                continue
            thesis_rows_by_ws.setdefault(str(r["workspace_id"]), []).append(dict(r))

        verdict_by_ws: dict[str, dict[str, Any]] = {}
        reval_verdict_by_ws: dict[str, dict[str, Any]] = {}
        for r in verdict_rows:
            ws_id = str(r["workspace_id"])
            row_dict = dict(r)
            row_key = _verdict_sort_key(row_dict)
            prev_any = verdict_by_ws.get(ws_id)
            if prev_any is None or row_key > _verdict_sort_key(prev_any):
                verdict_by_ws[ws_id] = row_dict
            if str(row_dict.get("word") or "") in REVALUATION_VERDICT_WORDS:
                prev_rev = reval_verdict_by_ws.get(ws_id)
                if prev_rev is None or row_key > _verdict_sort_key(prev_rev):
                    reval_verdict_by_ws[ws_id] = row_dict

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
            reval_verdict = reval_verdict_by_ws.get(ws_id)
            last_pub = pub_by_ws.get(ws_id)
            any_verdict_at = (
                _to_iso_timestamp(last_verdict.get("verdict_at"))
                or _to_iso_timestamp(last_verdict.get("created_at"))
            ) if last_verdict else None
            reval_verdict_at = (
                _to_iso_timestamp(reval_verdict.get("verdict_at"))
                or _to_iso_timestamp(reval_verdict.get("created_at"))
            ) if reval_verdict else None
            last_sv_increment_at = _extract_scenario_version_increment_at(
                thesis_rows_by_ws.get(ws_id) or [],
                latest_thesis_content=thesis_content if isinstance(thesis_content, dict) else None,
            )
            last_revaluation_at = _latest_iso_timestamp(
                last_sv_increment_at, reval_verdict_at
            )
            last_scan_at = _latest_iso_timestamp(
                any_verdict_at, last_sv_increment_at
            )
            scan_staleness = round(_compute_staleness(last_scan_at, today), 2)
            revaluation_staleness = round(
                _compute_staleness(last_revaluation_at, today), 2
            )
            scan_days = _days_since(last_scan_at, today)
            revaluation_days = _days_since(last_revaluation_at, today)
            scan_only_stale = bool(
                last_scan_at
                and (
                    last_revaluation_at is None
                    or (
                        revaluation_days is not None
                        and revaluation_days >= 14
                        and scan_days is not None
                        and revaluation_days > scan_days
                    )
                )
            )
            freshness_weight = round(max(revaluation_staleness, scan_staleness), 2)

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
                decision_impact = resolve_decision_impact(q)
                has_decision_impact = bool(decision_impact)
                open_questions_list.append(
                    {
                        "id": qid,
                        "question": q.get("question") or q.get("title") or qid,
                        "value": val,
                        "urgency": urg,
                        "backoff_until": backoff_until,
                        "waiting_pm": waiting_pm,
                        "decision_impact": decision_impact if has_decision_impact else None,
                    }
                )
                if not waiting_pm and not in_backoff and has_decision_impact:
                    v_w = VALUE_WEIGHT.get(val, 2.0)
                    u_w = URGENCY_WEIGHT.get(urg, 1.5)
                    score = round(v_w * u_w * freshness_weight, 2)
                    est_usd = DEFAULT_EST_COST_BY_KIND["open_question"]
                    raw_candidates.append(
                        {
                            "workspace_id": ws_id,
                            "ticker": ticker,
                            "task": f"跟进问题 {qid}：{q.get('question') or q.get('title') or qid}",
                            "kind": "open_question",
                            "score": score,
                            "est_usd": est_usd,
                            "decision_impact": decision_impact,
                            "last_scan_at": last_scan_at,
                            "last_revaluation_at": last_revaluation_at,
                            "scan_staleness": scan_staleness,
                            "revaluation_staleness": revaluation_staleness,
                            "scan_only_stale": scan_only_stale,
                            "why": (
                                f"价值={val} · 紧迫度={urg} · 最近扫描权重={scan_staleness:.2f} · "
                                f"最近投资重估权重={revaluation_staleness:.2f} · "
                                f"解决后改变的决定={decision_impact}"
                            ),
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
                if "decision_impact" in v and not normalize_decision_impact(
                    v.get("decision_impact")
                ):
                    continue
                v_decision = str(
                    v.get("decision_impact")
                    or v.get("expected")
                    or v.get("note")
                    or f"核验 {v_what} 是否达到阈值，决定是否调整方向（stance）、可执行度（actionability）或仓位"
                ).strip()
                # 与议程探针同一到期口径：保留原始时刻/时区，缺时区按标的交易所
                # 时区，不按本地（上海）日历日提前判到期。
                if validation_is_due(due_raw, ticker, now_dt, tz=market_tz):
                    score = round(3.0 * 3.0 * freshness_weight, 2)
                    raw_candidates.append(
                        {
                            "workspace_id": ws_id,
                            "ticker": ticker,
                            "task": f"验证节点到期 {vid}：{v_what}",
                            "kind": "validation_check",
                            "score": score,
                            "est_usd": DEFAULT_EST_COST_BY_KIND["validation_check"],
                            "decision_impact": v_decision,
                            "last_scan_at": last_scan_at,
                            "last_revaluation_at": last_revaluation_at,
                            "scan_staleness": scan_staleness,
                            "revaluation_staleness": revaluation_staleness,
                            "scan_only_stale": scan_only_stale,
                            "why": (
                                f"验证节点到期（{str(due_raw)[:10]}）· 最近扫描权重={scan_staleness:.2f} · "
                                f"最近投资重估权重={revaluation_staleness:.2f} · "
                                f"解决后改变的决定={v_decision}"
                            ),
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
                if "decision_impact" in s and not normalize_decision_impact(s.get("decision_impact")):
                    continue
                prio = str(s.get("priority") or "P1")
                v_w = VALUE_WEIGHT.get(prio, 2.0)
                score = round(v_w * 2.5 * freshness_weight, 2)
                s_decision = str(
                    s.get("decision_impact")
                    or f"评估信号 [{s.get('probe')}] 是否改变方向（stance）、可执行度（actionability）或仓位"
                ).strip()
                raw_candidates.append(
                    {
                        "workspace_id": ws_id,
                        "ticker": ticker,
                        "task": f"处理未消费信号 [{s.get('probe')}]：{s.get('summary')}",
                        "kind": "event_note",
                        "score": score,
                        "est_usd": DEFAULT_EST_COST_BY_KIND["event_note"],
                        "decision_impact": s_decision,
                        "last_scan_at": last_scan_at,
                        "last_revaluation_at": last_revaluation_at,
                        "scan_staleness": scan_staleness,
                        "revaluation_staleness": revaluation_staleness,
                        "scan_only_stale": scan_only_stale,
                        "why": (
                            f"未消费信号（{prio}）· 最近扫描权重={scan_staleness:.2f} · "
                            f"最近投资重估权重={revaluation_staleness:.2f} · "
                            f"解决后改变的决定={s_decision}"
                        ),
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
                    "last_scan_at": last_scan_at,
                    "last_revaluation_at": last_revaluation_at,
                    "scan_staleness": scan_staleness,
                    "revaluation_staleness": revaluation_staleness,
                    "scan_only_stale": scan_only_stale,
                    "pending_signals": len(ws_sigs),
                    "sec_probe_degraded": bool(
                        (watch_state_by_ws.get(ws_id) or {}).get("sec_probe_degraded")
                    ),
                }
            )

        # 行业层 P2：units 覆盖单元视图。开关关闭时本块整体不执行——
        # 不查 dalton_coverage_units / dalton_sector_members，输出不带
        # units 键。枚举、同事可见性与成员渲染都在 units_view（P4 抽出，
        # 与覆盖视图的 HTTP API 共享同一实现），board 只捕获失败降级。
        if units is not None:
            try:
                units.extend(await build_units_view(user_id, autos_by_user))
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
