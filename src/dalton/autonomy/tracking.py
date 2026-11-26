# ruff: noqa: BLE001, S110
"""Company workspace auto-tracking status, enable, and disable service."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row

from src.dalton.autonomy.schedule import (
    _prelock_unit_workspace,
    build_company_watch_specs,
    build_sector_watch_specs,
    list_all_user_automations,
    resolve_delta_schedule,
    schedule_dalton_automation_helper,
    user_automations_lock,
)
from src.dalton.coverage import get_user_tier
from src.dalton.services.company_automations import (
    automation_history_context,
    get_current_history_context,
)
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

LABEL_ENABLED_ZH = "自动跟踪：开启（盯盘每 30 分钟 · 内部资料北京时间 11:00 扫描，Core 每交易日、Watch 每周 · 涨跌 ±4% 触发）"
LABEL_DISABLED_ZH = "自动跟踪：关闭"
LABEL_INCOMPLETE_ZH = "自动跟踪：不完整"

# 行业单元（P3）：两类任务——议程到期（研究时区内每 2 小时）与资料增量
# （每周六 11:00，内部检索、无证券探针）。证券类事件按需订阅、公司侧发现
# 一次，行业默认不跑。
LABEL_SECTOR_ENABLED_ZH = "行业自动跟踪：开启（议程到期每 2 小时检查 · 内部资料每周六 11:00 增量扫描 · 不跑证券探针）"
LABEL_SECTOR_DISABLED_ZH = "行业自动跟踪：关闭"
LABEL_SECTOR_INCOMPLETE_ZH = "行业自动跟踪：不完整"


async def resolve_default_tier(
    ticker: str,
    explicit_tier: str | None = None,
    *,
    user_id: str | None = None,
    conn: Any = None,
) -> str:
    """Resolve tier for ``ticker`` from ``user_id``'s coverage roster or default to ``'watch'``.

    Only a ticker absent from the roster defaults to ``'watch'``. A failed lookup
    raises, because writing Watch's weekly schedule for a Core company is worse
    than failing the call. Pass ``conn`` when already holding a connection (the
    automations lock) so the lookup does not wait on a second one.
    """
    if explicit_tier and str(explicit_tier).strip():
        return str(explicit_tier).strip().lower()
    t_upper = str(ticker or "").strip().upper()
    if user_id and t_upper:
        user_tier = await get_user_tier(user_id, t_upper, conn=conn)
        if user_tier:
            return user_tier
    return "watch"


def _extract_ticker_from_text(raw: str | None) -> str | None:
    # Moved to src.dalton.coverage.identity (sector layer P0 round 2: one
    # resolver, one ticker rule). Kept as a re-export because knowledge.py and
    # tests import it from here.
    from src.dalton.coverage.identity import _extract_ticker_from_text as _impl

    return _impl(raw)


async def resolve_workspace_tracking_owner(
    workspace_id: str,
    workspace: Mapping[str, Any] | None = None,
    *,
    conn: Any = None,
    unit: Mapping[str, Any] | None = None,
) -> str:
    """Resolve effective task owner for ``workspace_id``: ``COALESCE(unit.lead_analyst_user_id, ws.user_id)``."""
    from src.dalton.settings import is_coverage_units_enabled

    ws_id = str(workspace_id or "").strip()
    if not ws_id:
        return ""

    if unit is not None:
        lead_uid = str(
            unit.get("lead_analyst_user_id")
            or unit.get("owner_user_id")
            or ""
        ).strip()
        if lead_uid:
            return lead_uid
    elif is_coverage_units_enabled():
        from src.dalton.database.coverage_units import get_active_unit_by_workspace

        try:
            resolved_unit = await get_active_unit_by_workspace(ws_id, conn=conn)
        except RuntimeError as exc:
            if "database pool is not open" not in str(exc):
                raise
            resolved_unit = None
        if resolved_unit is not None:
            lead_uid = str(
                resolved_unit.get("lead_analyst_user_id")
                or resolved_unit.get("owner_user_id")
                or ""
            ).strip()
            if lead_uid:
                return lead_uid

    ws_row = dict(workspace) if isinstance(workspace, Mapping) else await ws_db.get_workspace(ws_id, conn=conn)
    return str((ws_row or {}).get("user_id") or "").strip()


async def resolve_workspace_id_by_ticker(ticker: str, user_id: str) -> str | None:
    """Resolve workspace_id for ``ticker`` and ``user_id`` (prioritizing active units led by ``user_id``)."""
    from src.dalton.settings import is_coverage_units_enabled

    t_upper = str(ticker or "").strip().upper()
    uid = str(user_id or "").strip()
    if not t_upper or not uid:
        return None
    try:
        async with get_db_connection() as conn, conn.cursor() as cur:
            if is_coverage_units_enabled():
                from src.dalton.database.coverage_units import normalize_ticker_key

                t_key = normalize_ticker_key(t_upper) or t_upper
                await cur.execute(
                    """
                        SELECT u.workspace_id::text
                        FROM dalton_coverage_units u
                        JOIN workspaces w ON w.workspace_id = u.workspace_id
                        WHERE u.status = 'active'
                          AND u.kind = 'company'
                          AND u.workspace_id IS NOT NULL
                          AND COALESCE(NULLIF(BTRIM(u.lead_analyst_user_id), ''), u.owner_user_id) = %s
                          AND (
                              UPPER(BTRIM(COALESCE(u.ticker, ''))) = %s
                              OR UPPER(BTRIM(COALESCE(u.ticker_key, ''))) = %s
                          )
                          AND w.status <> 'deleted'
                        ORDER BY u.updated_at DESC NULLS LAST, w.created_at DESC
                        LIMIT 1
                        """,
                    (uid, t_upper, t_key.upper()),
                )
                unit_row = await cur.fetchone()
                if unit_row and unit_row[0]:
                    return str(unit_row[0])

            await cur.execute(
                """
                    SELECT workspace_id::text
                    FROM workspaces
                    WHERE UPPER(BTRIM(name)) = %s
                      AND user_id = %s
                      AND status <> 'deleted'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                (t_upper, uid),
            )
            row = await cur.fetchone()
            if row and row[0]:
                return str(row[0])

            await cur.execute(
                """
                    SELECT w.workspace_id::text
                    FROM workspaces w
                    JOIN dalton_knowledge_versions kv
                      ON kv.workspace_id = w.workspace_id
                     AND kv.doc_key = 'company'
                    WHERE w.user_id = %s
                      AND w.status <> 'deleted'
                      AND UPPER(BTRIM(kv.content->>'ticker')) = %s
                    ORDER BY kv.version_no DESC, w.created_at DESC
                    LIMIT 1
                    """,
                (uid, t_upper),
            )
            row2 = await cur.fetchone()
            if row2 and row2[0]:
                return str(row2[0])
    except Exception as exc:
        logger.debug(
            "[DALTON_TRACKING] Failed resolving workspace_id for ticker=%s: %s",
            t_upper,
            exc,
        )
    return None


async def resolve_workspace_id_by_ticker_any_owner(
    ticker: str,
    caller_user_id: str | None = None,
) -> str | None:
    """Resolve a workspace_id for ``ticker`` across owners the CALLER may read
    (round-3 decision 2).

    ``resolve_workspace_id_by_ticker`` filters by ``user_id``, so a colleague
    asking for the PM's workspace gets None and the tool would answer
    "not found". This twin lists every ticker match first and keeps the first
    one the caller can READ per ``sharing.require_workspace_access`` — the
    sharing rules cannot live in SQL. With no readable match it returns None:
    an unreadable workspace stays "not found", never owner-only-wording.
    """
    t_upper = str(ticker or "").strip().upper()
    if not t_upper:
        return None
    try:
        async with get_db_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    SELECT workspace_id::text
                    FROM workspaces
                    WHERE UPPER(BTRIM(name)) = %s
                      AND status <> 'deleted'
                    ORDER BY created_at DESC
                    """,
                (t_upper,),
            )
            rows = await cur.fetchall() or []
            await cur.execute(
                """
                    SELECT w.workspace_id::text
                    FROM workspaces w
                    JOIN dalton_knowledge_versions kv
                      ON kv.workspace_id = w.workspace_id
                     AND kv.doc_key = 'company'
                    WHERE w.status <> 'deleted'
                      AND UPPER(BTRIM(kv.content->>'ticker')) = %s
                    ORDER BY kv.version_no DESC, w.created_at DESC
                    """,
                (t_upper,),
            )
            rows.extend(await cur.fetchall() or [])
    except Exception as exc:
        logger.debug(
            "[DALTON_TRACKING] Failed resolving any-owner workspace_id for ticker=%s: %s",
            t_upper,
            exc,
        )
        return None

    caller = str(caller_user_id or "").strip()
    if not caller:
        return None
    from src.dalton.sharing import require_workspace_access

    seen_ids: set[str] = set()
    for r in rows:
        ws_id = str((r or {}).get("workspace_id") or "").strip()
        if not ws_id or ws_id in seen_ids:
            continue
        seen_ids.add(ws_id)
        try:
            ws = await ws_db.get_workspace(ws_id)
            if not ws:
                continue
            await require_workspace_access(
                ws, caller, channel="cos", need="read", workspace=ws
            )
            return ws_id
        except Exception:
            continue
    return None


async def _resolve_workspace_ticker(
    workspace_id: str,
    existing_rows: list[dict[str, Any]] | None = None,
    *,
    registered_units: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Resolve the uppercase company ticker for a workspace, or raise.

    Thin wrapper over the shared coverage-identity resolver (sector layer P0
    round 2): tracking, the board and the director probe all take "is this
    workspace a company / what is its ticker" from the same single resolver,
    so the three can no longer disagree. Home keeps its historical error
    message; a registered sector unit and an unclassified workspace (no
    metadata ticker, no company knowledge doc, no exact ticker name, no
    `dalton-<t>-*` automations) both raise — nothing is guessed.
    """
    ws: dict[str, Any] | None = None
    try:
        ws = await ws_db.get_workspace(str(workspace_id))
    except Exception as exc:
        logger.debug("[DALTON_TRACKING] get_workspace(%s) failed: %s", workspace_id, exc)

    from src.dalton.coverage.identity import resolve_workspace_coverage_identity

    identity = await resolve_workspace_coverage_identity(
        ws if ws is not None else {"workspace_id": str(workspace_id)},
        registered_units=registered_units,
        automations=existing_rows,
    )
    if identity.is_home:
        raise ValueError("Home workspace does not support company auto-tracking")
    if identity.is_sector:
        raise ValueError(
            f"Workspace '{workspace_id}' is a registered sector coverage unit; a sector never carries a ticker"
        )
    if not identity.is_company or not identity.ticker:
        raise ValueError(f"Unable to resolve company ticker for workspace '{workspace_id}'")
    return identity.ticker


def _filter_company_automations(
    rows: list[dict[str, Any]],
    *,
    workspace_id: str,
    ticker: str,
) -> list[dict[str, Any]]:
    """Filter ``automations`` rows belonging to this company workspace (`dalton-<t>-*`)."""
    ws_str = str(workspace_id).strip()
    prefix = f"dalton-{ticker.lower()}-"
    matched: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name.startswith(prefix):
            continue
        r_ws = str(r.get("workspace_id") or "").strip()
        if r_ws and r_ws != ws_str:
            continue
        matched.append(r)
    return matched


def _replaced_by_tracker(row: dict[str, Any] | None) -> str | None:
    """Non-empty tracker UUID on a row. Blank, missing, or non-string values are unmarked."""
    if not isinstance(row, dict):
        return None
    meta = row.get("metadata")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("replaced_by_tracker")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _is_expired_once_row(row: Mapping[str, Any]) -> bool:
    if str(row.get("trigger_type") or "").strip().lower() != "once":
        return False
    when = row.get("next_run_at")
    if when is None:
        return True
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        except ValueError:
            return True
    if not isinstance(when, datetime):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


def _derive_status_and_details(
    *,
    workspace_id: str,
    ticker: str,
    tier: str,
    company_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive tracking status (`enabled` | `disabled` | `incomplete`) and task list from `automations` rows."""
    specs = build_company_watch_specs(
        ticker=ticker,
        workspace_id=str(workspace_id),
        tier=tier,
    )
    expected_names = [str(s["name"]) for s in specs]
    tz = str(specs[0].get("timezone") or "America/New_York") if specs else "America/New_York"
    delta_name = f"dalton-{str(ticker).strip().lower()}-delta"

    by_name: dict[str, dict[str, Any]] = {}
    for r in company_rows:
        r_name = str(r.get("name") or "").strip()
        if r_name and r_name not in by_name:
            by_name[r_name] = r

    # Any same-name delta row can carry the marker. The first row alone is not enough.
    replaced_marker = None
    for r in company_rows:
        if str(r.get("name") or "").strip() != delta_name:
            continue
        replaced_marker = _replaced_by_tracker(r)
        if replaced_marker:
            break
    # A delta already replaced by the company tracker is not a missing or paused standard task.
    countable_names = [
        n for n in expected_names if not (n == delta_name and replaced_marker)
    ]
    existing_standard = [by_name[n] for n in countable_names if n in by_name]
    missing_tasks = [n for n in countable_names if n not in by_name]
    active_tasks = [
        n
        for n in countable_names
        if n in by_name and str(by_name[n].get("status") or "").lower() == "active"
    ]
    paused_tasks = [
        n
        for n in countable_names
        if n in by_name and str(by_name[n].get("status") or "").lower() == "paused"
    ]

    total_expected = len(countable_names)
    if len(expected_names) == 0:
        any_active_company = any(
            str(r.get("status") or "").lower() == "active"
            for r in company_rows
        )
        if any_active_company:
            status = "enabled"
            label = LABEL_ENABLED_ZH
        else:
            status = "disabled"
            label = LABEL_DISABLED_ZH
    elif len(existing_standard) == total_expected and len(active_tasks) == total_expected:
        status = "enabled"
        label = LABEL_ENABLED_ZH
    elif len(existing_standard) == 0 or (
        len(existing_standard) == total_expected and len(paused_tasks) == total_expected
    ):
        status = "disabled"
        label = LABEL_DISABLED_ZH
    else:
        status = "incomplete"
        label = LABEL_INCOMPLETE_ZH

    paused_by_user = any(
        isinstance(r.get("metadata"), dict) and bool(r["metadata"].get("paused_by_user"))
        for r in existing_standard
    ) or any(
        isinstance(r.get("metadata"), dict)
        and bool(r["metadata"].get("paused_by_user"))
        and not _is_expired_once_row(r)
        and not (replaced_marker and str(r.get("name") or "").strip() == delta_name)
        for r in company_rows
    )

    tasks_list: list[dict[str, Any]] = []
    for spec in specs:
        s_name = str(spec["name"])
        row = by_name.get(s_name)
        next_run = row.get("next_run_at") if row else None
        next_run_str = (
            next_run.isoformat()
            if hasattr(next_run, "isoformat")
            else (str(next_run) if next_run else None)
        )
        r_meta = (row.get("metadata") if row and isinstance(row.get("metadata"), dict) else None) or {}
        tasks_list.append(
            {
                "automation_id": str(row["automation_id"]) if row and row.get("automation_id") else None,
                "name": s_name,
                "description": (row.get("description") if row else None) or spec.get("description"),
                "trigger_type": (row.get("trigger_type") if row else None) or spec.get("trigger_type"),
                "cron_expression": (row.get("cron_expression") if row else None) or spec.get("cron_expression"),
                "timezone": (row.get("timezone") if row else None) or spec.get("timezone"),
                "status": str(row.get("status")) if row and row.get("status") else "missing",
                "exists": row is not None,
                "next_run_at": next_run_str,
                "paused_by_user": bool(r_meta.get("paused_by_user")),
                "standard": True,
            }
        )

    for r in company_rows:
        r_name = str(r.get("name") or "").strip()
        if r_name in expected_names:
            continue
        next_run = r.get("next_run_at")
        next_run_str = (
            next_run.isoformat()
            if hasattr(next_run, "isoformat")
            else (str(next_run) if next_run else None)
        )
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        tasks_list.append(
            {
                "automation_id": str(r["automation_id"]) if r.get("automation_id") else None,
                "name": r_name,
                "description": r.get("description"),
                "trigger_type": r.get("trigger_type"),
                "cron_expression": r.get("cron_expression"),
                "timezone": r.get("timezone") or tz,
                "status": str(r.get("status") or "active"),
                "exists": True,
                "next_run_at": next_run_str,
                "paused_by_user": bool(r_meta.get("paused_by_user")),
                "standard": False,
            }
        )

    details = {
        "ok": True,
        "workspace_id": str(workspace_id),
        "ticker": ticker,
        "tier": tier,
        "timezone": tz,
        "status": status,
        "enabled": status == "enabled",
        "paused_by_user": paused_by_user,
        "label": label,
        "missing_tasks": missing_tasks,
        "paused_tasks": paused_tasks,
        "active_tasks": active_tasks,
        "tasks": tasks_list,
        "automations": tasks_list,
    }
    if replaced_marker:
        details["delta_replaced_by_tracker"] = replaced_marker
    return details


def _merge_unit_managed_rows(
    base_rows: list[dict[str, Any]],
    *,
    candidates: Sequence[Any],
    workspace_id: str,
    unit: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Merge unit-managed rows (earnings ticker aliases, renamed `unit_id` rows) into ``base_rows``."""
    if unit is None:
        return [dict(r) for r in base_rows if isinstance(r, Mapping)]
    unit_id = str(unit.get("unit_id") or "").strip()
    extras = _filter_unit_managed_rows(
        candidates,
        clean_ws=str(workspace_id or "").strip(),
        clean_unit_id=unit_id,
        eff_unit=unit,
    )
    out = [dict(r) for r in base_rows if isinstance(r, Mapping)]
    seen_ids = {
        str(r.get("automation_id") or "").strip()
        for r in out
        if r.get("automation_id")
    }
    seen_names = {
        str(r.get("name") or "").strip()
        for r in out
        if not r.get("automation_id") and r.get("name")
    }
    for er in extras:
        eid = str(er.get("automation_id") or "").strip()
        ename = str(er.get("name") or "").strip()
        if eid:
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
        elif ename:
            if ename in seen_names:
                continue
            seen_names.add(ename)
        out.append(er)
    return out


async def _resolve_company_unit_for_load(
    workspace_id: str,
    *,
    registered_units: Mapping[str, Mapping[str, Any]] | None,
    unit: Mapping[str, Any] | None,
    conn: Any,
) -> Mapping[str, Any] | None:
    if unit is not None:
        return unit if str(unit.get("kind") or "").strip().lower() == "company" else None
    ws_str = str(workspace_id or "").strip()
    if not ws_str:
        return None
    if registered_units is not None:
        cand = registered_units.get(ws_str)
        if cand is not None and str(cand.get("kind") or "").strip().lower() == "company":
            return cand
        return None
    from src.dalton.settings import is_coverage_units_enabled

    if not is_coverage_units_enabled():
        return None
    from src.dalton.database.coverage_units import get_active_unit_by_workspace

    try:
        resolved = await get_active_unit_by_workspace(ws_str, conn=conn)
    except RuntimeError as exc:
        if "database pool is not open" not in str(exc):
            raise
        return None
    if resolved is not None and str(resolved.get("kind") or "").strip().lower() == "company":
        return resolved
    return None


async def _load_company_automations(
    user_id: str,
    workspace_id: str,
    *,
    ticker: str | None = None,
    registered_units: Mapping[str, Mapping[str, Any]] | None = None,
    unit: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve ``ticker`` and load unit-managed + ``dalton-<ticker>-*`` automation rows."""
    resolved_ticker = ticker
    if not resolved_ticker:
        try:
            resolved_ticker = await _resolve_workspace_ticker(
                workspace_id,
                registered_units=registered_units,
            )
        except ValueError:
            all_rows = await list_all_user_automations(
                user_id,
                conn=conn,
            )
            resolved_ticker = await _resolve_workspace_ticker(
                workspace_id,
                existing_rows=all_rows,
                registered_units=registered_units,
            )
            matched = _filter_company_automations(
                all_rows,
                workspace_id=workspace_id,
                ticker=resolved_ticker,
            )
            eff_unit = await _resolve_company_unit_for_load(
                workspace_id, registered_units=registered_units, unit=unit, conn=conn
            )
            return resolved_ticker, _merge_unit_managed_rows(
                matched, candidates=all_rows, workspace_id=workspace_id, unit=eff_unit
            )

    eff_unit = await _resolve_company_unit_for_load(
        workspace_id, registered_units=registered_units, unit=unit, conn=conn
    )
    prefix = f"dalton-{resolved_ticker.lower()}-"
    if eff_unit is not None:
        all_rows = await list_all_user_automations(
            user_id,
            conn=conn,
        )
        matched = _filter_company_automations(
            all_rows,
            workspace_id=workspace_id,
            ticker=resolved_ticker,
        )
        return resolved_ticker, _merge_unit_managed_rows(
            matched, candidates=all_rows, workspace_id=workspace_id, unit=eff_unit
        )

    prefixed_rows = await list_all_user_automations(
        user_id,
        name_prefix=prefix,
        conn=conn,
    )
    return resolved_ticker, _filter_company_automations(
        prefixed_rows,
        workspace_id=workspace_id,
        ticker=resolved_ticker,
    )


async def _resolve_workspace_sector(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """行业单元解析（P3）：工作区绑定 active sector 单元时返回
    ``{"unit", "tz", "tz_source"}``，否则 None。

    与公司路径共用 P0 的身份解析器：开关关闭时它不查注册表，本函数因此
    也不会为老工作区引入任何新查询。到期时区按 autonomy.sector 的口径
    解析（sector 文档 research_tz → 用户时区 → 部署默认），供两类行业
    任务共用。"""
    ws: dict[str, Any] | None = None
    try:
        ws = await ws_db.get_workspace(str(workspace_id))
    except Exception as exc:
        logger.debug("[DALTON_TRACKING] get_workspace(%s) failed: %s", workspace_id, exc)
    if ws is None:
        return None

    from src.dalton.autonomy.sector import resolve_sector_due_timezone
    from src.dalton.coverage.identity import resolve_workspace_coverage_identity

    identity = await resolve_workspace_coverage_identity(ws, conn=conn)
    if not identity.is_sector or identity.unit is None:
        return None
    unit = dict(identity.unit)
    tz_name, tz_source = await resolve_sector_due_timezone(
        workspace_id=str(workspace_id),
        user_id=str(
            unit.get("lead_analyst_user_id") or unit.get("owner_user_id") or ""
        ),
        conn=conn,
    )
    return {"unit": unit, "tz": tz_name, "tz_source": tz_source}


def _filter_sector_automations(
    rows: list[dict[str, Any]],
    *,
    unit_id: str,
) -> list[dict[str, Any]]:
    """Filter ``automations`` rows belonging to this sector unit.

    Ownership is the unit identity, not the workspace (codex r2 #1): a row
    belongs to the unit when its name starts with ``dalton-sector-`` and its
    metadata carries the unit's ``unit_id`` — so tasks left on a workspace the
    unit has since been rebound away from still count, and pausing the sector
    reaches them wherever they sit. The company filter's ``dalton-<ticker>-``
    prefix never matches these names, so a sector's tasks and a member
    company's tasks cannot leak into each other's views.
    """
    uid = str(unit_id or "").strip()
    matched: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not str(r.get("name") or "").strip().lower().startswith("dalton-sector-"):
            continue
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if str(r_meta.get("unit_id") or "").strip() != uid:
            continue
        matched.append(r)
    return matched


def _derive_sector_status_and_details(
    *,
    workspace_id: str,
    unit: dict[str, Any],
    tz_name: str,
    tz_source: str,
    sector_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a sector unit's tracking status from its unit-owned automation rows.

    ``workspace_id`` is the unit's CURRENT binding. A standard task satisfies
    the suite only when its row sits on the current workspace (or carries no
    workspace, the legacy rule); rows the unit left behind on an older
    workspace surface as extra ``stale_unit_binding`` tasks instead of
    counting towards enabled.
    """
    slug = str(unit.get("slug") or "").strip()
    display_name = str(unit.get("display_name") or slug).strip()
    specs = build_sector_watch_specs(
        unit_id=str(unit.get("unit_id") or ""),
        slug=slug,
        display_name=display_name,
        workspace_id=str(workspace_id),
        timezone_name=tz_name,
    )
    expected_names = [str(s["name"]) for s in specs]
    tz = str(specs[0].get("timezone") or tz_name) if specs else tz_name
    ws_str = str(workspace_id).strip()

    def _row_is_current(r: dict[str, Any]) -> bool:
        row_ws = str(r.get("workspace_id") or "").strip()
        return not row_ws or row_ws == ws_str

    by_name: dict[str, dict[str, Any]] = {}
    for r in sector_rows:
        r_name = str(r.get("name") or "").strip()
        if not r_name:
            continue
        existing = by_name.get(r_name)
        # 同名行取当前工作区上的那份；换绑遗留的旧行不抢占标准任务位，
        # 只在下方的额外任务里以 stale 面目出现。
        if existing is None or (_row_is_current(r) and not _row_is_current(existing)):
            by_name[r_name] = r

    existing_standard = [
        by_name[n] for n in expected_names if n in by_name and _row_is_current(by_name[n])
    ]
    missing_tasks = [
        n for n in expected_names if n not in by_name or not _row_is_current(by_name[n])
    ]
    active_tasks = [
        n
        for n in expected_names
        if n in by_name
        and _row_is_current(by_name[n])
        and str(by_name[n].get("status") or "").lower() == "active"
    ]
    paused_tasks = [
        n
        for n in expected_names
        if n in by_name
        and _row_is_current(by_name[n])
        and str(by_name[n].get("status") or "").lower() == "paused"
    ]

    total_expected = len(expected_names)
    if len(existing_standard) == total_expected and len(active_tasks) == total_expected:
        status = "enabled"
        label = LABEL_SECTOR_ENABLED_ZH
    elif len(existing_standard) == 0 or (
        len(existing_standard) == total_expected and len(paused_tasks) == total_expected
    ):
        status = "disabled"
        label = LABEL_SECTOR_DISABLED_ZH
    else:
        status = "incomplete"
        label = LABEL_SECTOR_INCOMPLETE_ZH

    paused_by_user = any(
        isinstance(r.get("metadata"), dict) and bool(r["metadata"].get("paused_by_user"))
        for r in sector_rows
    )

    tasks_list: list[dict[str, Any]] = []
    chosen_row_ids: set[int] = set()
    for spec in specs:
        s_name = str(spec["name"])
        row = by_name.get(s_name)
        if row is not None:
            chosen_row_ids.add(id(row))
        next_run = row.get("next_run_at") if row else None
        next_run_str = (
            next_run.isoformat()
            if hasattr(next_run, "isoformat")
            else (str(next_run) if next_run else None)
        )
        r_meta = (row.get("metadata") if row and isinstance(row.get("metadata"), dict) else None) or {}
        row_stale = bool(row) and not _row_is_current(row)
        tasks_list.append(
            {
                "automation_id": str(row["automation_id"]) if row and row.get("automation_id") else None,
                "name": s_name,
                "description": (row.get("description") if row else None) or spec.get("description"),
                "trigger_type": (row.get("trigger_type") if row else None) or spec.get("trigger_type"),
                "cron_expression": (row.get("cron_expression") if row else None) or spec.get("cron_expression"),
                "timezone": (row.get("timezone") if row else None) or spec.get("timezone"),
                "status": str(row.get("status")) if row and row.get("status") else "missing",
                "exists": row is not None,
                "next_run_at": next_run_str,
                "paused_by_user": bool(r_meta.get("paused_by_user")),
                "workspace_id": (str(row.get("workspace_id")) if row and row.get("workspace_id") else None),
                "stale_unit_binding": row_stale,
                "standard": True,
            }
        )
    for r in sector_rows:
        if id(r) in chosen_row_ids:
            continue
        r_name = str(r.get("name") or "").strip()
        next_run = r.get("next_run_at")
        next_run_str = (
            next_run.isoformat()
            if hasattr(next_run, "isoformat")
            else (str(next_run) if next_run else None)
        )
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        tasks_list.append(
            {
                "automation_id": str(r["automation_id"]) if r.get("automation_id") else None,
                "name": r_name,
                "description": r.get("description"),
                "trigger_type": r.get("trigger_type"),
                "cron_expression": r.get("cron_expression"),
                "timezone": r.get("timezone") or tz,
                "status": str(r.get("status") or "active"),
                "exists": True,
                "next_run_at": next_run_str,
                "paused_by_user": bool(r_meta.get("paused_by_user")),
                "workspace_id": (str(r.get("workspace_id")) if r.get("workspace_id") else None),
                "stale_unit_binding": not _row_is_current(r),
                "standard": False,
            }
        )

    return {
        "ok": True,
        "workspace_id": str(workspace_id),
        "subject_kind": "sector",
        "unit_id": str(unit.get("unit_id") or "") or None,
        "sector_slug": slug,
        "display_name": display_name,
        "ticker": "",
        "tier": "",
        "timezone": tz,
        "timezone_source": tz_source,
        "status": status,
        "enabled": status == "enabled",
        "paused_by_user": paused_by_user,
        "label": label,
        "missing_tasks": missing_tasks,
        "paused_tasks": paused_tasks,
        "active_tasks": active_tasks,
        "stale_tasks": [t["name"] for t in tasks_list if t.get("stale_unit_binding")],
        "tasks": tasks_list,
        "automations": tasks_list,
    }


async def _load_sector_automations(
    user_id: str,
    unit_id: str,
    *,
    workspace_id: str | None = None,
    unit: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Load every automation row owned by this sector unit (including renamed `unit_id` rows)."""
    from src.dalton.settings import is_coverage_units_enabled

    if is_coverage_units_enabled() and unit is not None and str(unit_id or "").strip():
        all_rows = await list_all_user_automations(user_id, conn=conn)
        base = _filter_sector_automations(all_rows, unit_id=unit_id)
        return _merge_unit_managed_rows(
            base,
            candidates=all_rows,
            workspace_id=str(workspace_id or "").strip(),
            unit=unit,
        )

    rows = await list_all_user_automations(
        user_id,
        name_prefix="dalton-sector-",
        conn=conn,
    )
    return _filter_sector_automations(rows, unit_id=unit_id)


async def _pause_stale_sector_rows(
    user_id: str,
    unit_id: str,
    current_workspace_id: str,
    *,
    rows: list[dict[str, Any]] | None = None,
    actor_user_id: str | None = None,
    conn: Any = None,
) -> list[str]:
    """Pause this unit's rows that sit on a workspace it is no longer bound to.

    codex r2 #1: after a rebind the old workspace's tasks would keep waking a
    thread that no longer represents the sector. Pausing (not deleting — the
    row and its history stay) with ``metadata.stale_unit_binding`` records why;
    preflight skips and pauses them too if they fire before any tracking call.
    """
    uid = str(user_id or "").strip()
    ws_str = str(current_workspace_id or "").strip()
    effective_rows = (
        list(rows)
        if rows is not None
        else await _load_sector_automations(uid, unit_id, conn=conn)
    )
    paused: list[str] = []
    conn_kw = {"conn": conn} if conn is not None else {}
    outer_ctx = get_current_history_context()
    resolved_actor = (
        str(actor_user_id).strip()
        if actor_user_id is not None and str(actor_user_id).strip()
        else (
            str(outer_ctx.actor_user_id).strip()
            if outer_ctx is not None
            and outer_ctx.has_explicit_actor
            and outer_ctx.actor_user_id
            else None
        )
    )
    with automation_history_context(
        actor_user_id=resolved_actor,
        action="maintenance_pause_stale_binding",
        skip_if_unchanged=True,
    ):
        for r in effective_rows:
            row_ws = str(r.get("workspace_id") or "").strip()
            if not row_ws or row_ws == ws_str:
                continue
            auto_id = r.get("automation_id")
            if not auto_id:
                continue
            new_meta = dict(r.get("metadata") or {})
            new_meta["stale_unit_binding"] = True
            await auto_db.update_automation(
                str(auto_id),
                uid,
                status="paused",
                next_run_at=None,
                metadata=new_meta,
                **conn_kw,
            )
            paused.append(str(r.get("name") or auto_id))
    return paused


def _require_valid_sector_automation_names(slug: str) -> None:
    """Refuse up front when the sector's names cannot be valid automation names.

    A 50+ char slug overflows the `dalton-<slug64>` name rule; failing here
    with a clear message beats a silent helper error and a forever-missing
    task in the status view.
    """
    from src.dalton.autonomy.schedule import validate_automation_name

    for suffix in ("agenda", "delta"):
        name = f"dalton-sector-{slug}-{suffix}"
        valid, err = validate_automation_name(name)
        if not valid:
            raise ValueError(
                f"sector slug {slug!r} produces an invalid automation name {name!r}: {err}"
            )


_SYSTEM_JOB_VIEWER_USER_ID: ContextVar[str | None] = ContextVar(
    "_SYSTEM_JOB_VIEWER_USER_ID", default=None
)


@contextmanager
def system_job_viewer_context(viewer_user_id: str | None) -> Iterator[None]:
    """Propagate the caller user_id into tracking status derivation without changing mock signatures."""
    cleaned = str(viewer_user_id or "").strip() or None
    token = _SYSTEM_JOB_VIEWER_USER_ID.set(cleaned)
    try:
        yield
    finally:
        _SYSTEM_JOB_VIEWER_USER_ID.reset(token)


async def _filter_rows_by_viewer_web_read(
    rows: list[dict[str, Any]],
    *,
    current_workspace_id: str,
    viewer_user_id: str | None,
    check_current_workspace: bool = False,
) -> list[dict[str, Any]]:
    """Omit automation rows whose bound workspace lacks Web read access for the viewer."""
    viewer = str(viewer_user_id or "").strip()
    if not viewer:
        return rows
    ws_str = str(current_workspace_id or "").strip()
    from src.dalton.sharing import require_workspace_access

    cache: dict[str, bool] = {}
    kept: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        r_ws = str(r.get("workspace_id") or "").strip()
        if not r_ws:
            kept.append(r)
            continue
        if not check_current_workspace and r_ws == ws_str:
            kept.append(r)
            continue
        readable = cache.get(r_ws)
        if readable is None:
            try:
                await require_workspace_access(r_ws, viewer, channel="web", need="read")
                readable = True
            except Exception:
                readable = False
            cache[r_ws] = readable
        if readable:
            kept.append(r)
    return kept


async def get_tracking_status(
    user_id: str,
    workspace_id: str,
    *,
    fallback_on_registry_error: bool = False,
    viewer_user_id: str | None = None,
) -> dict[str, Any]:
    """Derive the company workspace tracking status (`enabled` | `disabled` | `incomplete` | `not_applicable`) from `automations`."""
    from src.dalton.coverage.identity import CoverageIdentityUnavailableError

    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    effective_viewer = (
        str(viewer_user_id).strip()
        if viewer_user_id is not None and str(viewer_user_id).strip()
        else _SYSTEM_JOB_VIEWER_USER_ID.get()
    )
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    identity_unavailable = False
    try:
        ticker, company_rows = await _load_company_automations(uid, ws_id)
    except CoverageIdentityUnavailableError:
        if not fallback_on_registry_error:
            raise
        identity_unavailable = True
        try:
            ticker, company_rows = await _load_company_automations(
                uid, ws_id, registered_units={}
            )
        except ValueError:
            return {
                "ok": True,
                "workspace_id": ws_id,
                "ticker": "",
                "tier": "",
                "timezone": "America/New_York",
                "status": "not_applicable",
                "enabled": False,
                "paused_by_user": False,
                "label": "",
                "missing_tasks": [],
                "paused_tasks": [],
                "active_tasks": [],
                "tasks": [],
                "automations": [],
                "identity_unavailable": True,
            }
    except ValueError:
        # 公司身份解析失败：已登记行业单元（开关打开时）给出行业跟踪状态；
        # 其余保持 not_applicable。开关关闭时 _resolve_workspace_sector 不查
        # 注册表，老工作区的行为与 P3 前一致。
        try:
            sector = await _resolve_workspace_sector(ws_id)
        except CoverageIdentityUnavailableError:
            if not fallback_on_registry_error:
                raise
            return {
                "ok": True,
                "workspace_id": ws_id,
                "ticker": "",
                "tier": "",
                "timezone": "America/New_York",
                "status": "not_applicable",
                "enabled": False,
                "paused_by_user": False,
                "label": "",
                "missing_tasks": [],
                "paused_tasks": [],
                "active_tasks": [],
                "tasks": [],
                "automations": [],
                "identity_unavailable": True,
            }
        if sector is not None:
            from src.dalton.settings import is_coverage_units_enabled

            if effective_viewer and is_coverage_units_enabled():
                from src.dalton.sharing import require_workspace_access

                await require_workspace_access(
                    ws_id, effective_viewer, channel="web", need="read"
                )
            sector_rows = await _load_sector_automations(
                uid,
                str(sector["unit"].get("unit_id") or ""),
                workspace_id=ws_id,
                unit=sector["unit"],
            )
            if effective_viewer and is_coverage_units_enabled():
                sector_rows = await _filter_rows_by_viewer_web_read(
                    sector_rows,
                    current_workspace_id=ws_id,
                    viewer_user_id=effective_viewer,
                    check_current_workspace=True,
                )
            return _derive_sector_status_and_details(
                workspace_id=ws_id,
                unit=sector["unit"],
                tz_name=sector["tz"],
                tz_source=sector["tz_source"],
                sector_rows=sector_rows,
            )
        return {
            "ok": True,
            "workspace_id": ws_id,
            "ticker": "",
            "tier": "",
            "timezone": "America/New_York",
            "status": "not_applicable",
            "enabled": False,
            "paused_by_user": False,
            "label": "",
            "missing_tasks": [],
            "paused_tasks": [],
            "active_tasks": [],
            "tasks": [],
            "automations": [],
        }
    from src.dalton.settings import is_coverage_units_enabled

    if effective_viewer and is_coverage_units_enabled():
        from src.dalton.sharing import require_workspace_access

        await require_workspace_access(ws_id, effective_viewer, channel="web", need="read")
        company_rows = await _filter_rows_by_viewer_web_read(
            company_rows,
            current_workspace_id=ws_id,
            viewer_user_id=effective_viewer,
            check_current_workspace=True,
        )
    tier = await resolve_default_tier(ticker, user_id=uid)
    payload = _derive_status_and_details(
        workspace_id=ws_id,
        ticker=ticker,
        tier=tier,
        company_rows=company_rows,
    )
    if identity_unavailable:
        payload["identity_unavailable"] = True
    return payload


async def _enable_sector_tracking(
    user_id: str,
    workspace_id: str,
    sector: dict[str, Any],
    *,
    force: bool = False,
    respect_user_pause: bool = True,
    viewer_user_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Idempotently create or resume a sector unit's two standard tasks (P3).

    Ownership is the unit identity: rows this unit left on an older workspace
    are paused first (codex r2 #1), then the suite is upserted on the unit's
    current workspace. Member companies are never touched — the specs address
    `dalton-sector-<slug>-*` names, disjoint from `dalton-<ticker>-*`.
    """
    from src.dalton.settings import is_coverage_units_enabled

    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    effective_viewer = (
        str(viewer_user_id).strip()
        if viewer_user_id is not None and str(viewer_user_id).strip()
        else _SYSTEM_JOB_VIEWER_USER_ID.get()
    )
    if effective_viewer and is_coverage_units_enabled():
        from src.dalton.sharing import require_workspace_access

        await require_workspace_access(ws_id, effective_viewer, channel="web", need="read")
    unit = sector["unit"]
    slug = str(unit.get("slug") or "").strip()
    unit_id = str(unit.get("unit_id") or "").strip()
    _require_valid_sector_automation_names(slug)

    stale_paused = await _pause_stale_sector_rows(uid, unit_id, ws_id, conn=conn)
    if stale_paused:
        logger.info(
            "[DALTON_TRACKING] paused %d stale sector task(s) for unit %s after rebind: %s",
            len(stale_paused),
            unit_id,
            ", ".join(stale_paused),
        )

    sector_rows = await _load_sector_automations(
        uid, unit_id, workspace_id=ws_id, unit=unit, conn=conn
    )
    raw_paused = any(
        isinstance(r.get("metadata"), dict)
        and bool(r["metadata"].get("paused_by_user"))
        and not _is_expired_once_row(r)
        for r in sector_rows
    )
    visible_sector_rows = (
        await _filter_rows_by_viewer_web_read(
            sector_rows,
            current_workspace_id=ws_id,
            viewer_user_id=effective_viewer,
            check_current_workspace=True,
        )
        if effective_viewer
        else sector_rows
    )
    current = _derive_sector_status_and_details(
        workspace_id=ws_id,
        unit=unit,
        tz_name=sector["tz"],
        tz_source=sector["tz_source"],
        sector_rows=visible_sector_rows,
    )
    if not force and respect_user_pause and raw_paused:
        return {
            **current,
            "paused_by_user": True,
            "skipped": True,
            "skip_reason": "paused_by_user",
        }

    specs = build_sector_watch_specs(
        unit_id=unit_id,
        slug=slug,
        display_name=str(unit.get("display_name") or slug),
        workspace_id=ws_id,
        timezone_name=sector["tz"],
    )
    expected_sector_names = {str(s["name"]) for s in specs}
    outer_ctx = get_current_history_context()
    effective_actor = (
        str(outer_ctx.actor_user_id).strip()
        if outer_ctx is not None
        and outer_ctx.has_explicit_actor
        and outer_ctx.actor_user_id
        else uid
    )
    by_name_current: dict[str, dict[str, Any]] = {}
    for r in sector_rows:
        if isinstance(r, dict) and r.get("name"):
            row_ws = str(r.get("workspace_id") or "").strip()
            if not row_ws or row_ws == ws_id:
                by_name_current[str(r["name"]).strip()] = r

    for spec in specs:
        spec_copy = dict(spec)
        s_name = str(spec_copy["name"])
        existing_s = by_name_current.get(s_name)
        if existing_s and _replaced_by_tracker(existing_s):
            continue
        spec_meta = dict(spec_copy.get("metadata") or {})
        spec_meta["paused_by_user"] = None
        spec_copy["metadata"] = spec_meta
        spec_copy["status"] = "active"
        was_paused = bool(
            existing_s and str(existing_s.get("status") or "").strip().lower() == "paused"
        )
        with automation_history_context(
            actor_user_id=effective_actor,
            action="resume" if was_paused else "update",
            skip_if_unchanged=True,
        ):
            await schedule_dalton_automation_helper(
                s_name,
                spec_copy,
                user_id=uid,
                workspace_id=ws_id,
                ticker=None,
                role="chief_of_staff",
                action="upsert",
                conn=conn,
            )

    resume_refusals: list[dict[str, str]] = []
    for r in sector_rows:
        r_name = str(r.get("name") or "").strip()
        row_ws = str(r.get("workspace_id") or "").strip()
        if row_ws and row_ws != ws_id:
            continue
        if r_name in expected_sector_names or _replaced_by_tracker(r):
            continue
        if str(r.get("status") or "").lower() == "paused" and r.get("automation_id"):
            resume_res = await schedule_dalton_automation_helper(
                r_name,
                {"action": "resume", "automation_id": str(r["automation_id"])},
                user_id=uid,
                workspace_id=ws_id,
                ticker=None,
                role="chief_of_staff",
                action="resume",
                actor_user_id=effective_actor,
                conn=conn,
            )
            if not resume_res.get("ok"):
                resume_refusals.append(
                    {
                        "automation_id": str(r["automation_id"]),
                        "name": r_name,
                        "error": str(resume_res.get("error") or "resume_refused"),
                        "message": str(
                            resume_res.get("message") or resume_res.get("error") or ""
                        ),
                    }
                )

    updated_rows = await _load_sector_automations(
        uid, unit_id, workspace_id=ws_id, unit=unit, conn=conn
    )
    if effective_viewer:
        updated_rows = await _filter_rows_by_viewer_web_read(
            updated_rows,
            current_workspace_id=ws_id,
            viewer_user_id=effective_viewer,
            check_current_workspace=True,
        )
    result = _derive_sector_status_and_details(
        workspace_id=ws_id,
        unit=unit,
        tz_name=sector["tz"],
        tz_source=sector["tz_source"],
        sector_rows=updated_rows,
    )
    if resume_refusals:
        result["resume_refusals"] = resume_refusals
    if stale_paused:
        visible_stale = (
            stale_paused
            if not effective_viewer
            else [t["name"] for t in result.get("tasks") or [] if t.get("stale_unit_binding")]
        )
        if visible_stale:
            result["paused_stale_tasks"] = visible_stale
    return result


async def _disable_sector_tracking(
    user_id: str,
    workspace_id: str,
    sector: dict[str, Any],
    *,
    viewer_user_id: str | None = None,
    paused_ids_out: list[str] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Pause a sector unit's tasks without deleting them — on ANY workspace.

    codex r2 #1: the filter is the unit identity, so a rebind cannot leave the
    old workspace's agenda/delta running while the new one reports "关闭".
    Rows on other workspaces additionally get ``stale_unit_binding`` so the
    pause reason is diagnosable. Missing standard tasks are created paused on
    the current workspace with ``paused_by_user = True``, mirroring the
    company disable contract. Member companies' rows are never matched.
    """
    from src.dalton.settings import is_coverage_units_enabled

    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    effective_viewer = (
        str(viewer_user_id).strip()
        if viewer_user_id is not None and str(viewer_user_id).strip()
        else _SYSTEM_JOB_VIEWER_USER_ID.get()
    )
    if effective_viewer and is_coverage_units_enabled():
        from src.dalton.sharing import require_workspace_access

        await require_workspace_access(ws_id, effective_viewer, channel="web", need="read")
    unit = sector["unit"]
    slug = str(unit.get("slug") or "").strip()
    unit_id = str(unit.get("unit_id") or "").strip()
    _require_valid_sector_automation_names(slug)

    sector_rows = await _load_sector_automations(
        uid, unit_id, workspace_id=ws_id, unit=unit, conn=conn
    )
    by_name_current: dict[str, dict[str, Any]] = {}
    for r in sector_rows:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        row_ws = str(r.get("workspace_id") or "").strip()
        if not row_ws or row_ws == ws_id:
            by_name_current[str(r.get("name") or "").strip()] = r

    paused_names: list[str] = []
    conn_kw = {"conn": conn} if conn is not None else {}
    outer_ctx = get_current_history_context()
    effective_actor = (
        str(outer_ctx.actor_user_id).strip()
        if outer_ctx is not None
        and outer_ctx.has_explicit_actor
        and outer_ctx.actor_user_id
        else uid
    )
    with automation_history_context(
        actor_user_id=effective_actor,
        action="pause",
        skip_if_unchanged=True,
    ):
        for r in sector_rows:
            auto_id = r.get("automation_id")
            if not auto_id:
                continue
            row_ws = str(r.get("workspace_id") or "").strip()
            new_meta = dict(r.get("metadata") or {})
            new_meta["paused_by_user"] = True
            if row_ws and row_ws != ws_id:
                new_meta["stale_unit_binding"] = True
            pause_kwargs: dict[str, Any] = {
                "status": "paused",
                "metadata": new_meta,
            }
            if str(r.get("trigger_type") or "").strip().lower() != "once":
                pause_kwargs["next_run_at"] = None
            await auto_db.update_automation(
                str(auto_id),
                uid,
                **pause_kwargs,
                **conn_kw,
            )
            paused_names.append(str(r.get("name") or auto_id))
            if paused_ids_out is not None:
                paused_ids_out.append(str(auto_id))

    specs = build_sector_watch_specs(
        unit_id=unit_id,
        slug=slug,
        display_name=str(unit.get("display_name") or slug),
        workspace_id=ws_id,
        timezone_name=sector["tz"],
    )
    with automation_history_context(
        actor_user_id=effective_actor,
        action="create",
        skip_if_unchanged=True,
    ):
        for spec in specs:
            s_name = str(spec["name"])
            if s_name in by_name_current:
                continue
            spec_copy = dict(spec)
            spec_meta = dict(spec_copy.get("metadata") or {})
            spec_meta["paused_by_user"] = True
            spec_copy["metadata"] = spec_meta
            spec_copy["status"] = "paused"
            await schedule_dalton_automation_helper(
                s_name,
                spec_copy,
                user_id=uid,
                workspace_id=ws_id,
                ticker=None,
                role="chief_of_staff",
                action="upsert",
                conn=conn,
            )
            paused_names.append(s_name)

    updated_rows = await _load_sector_automations(
        uid, unit_id, workspace_id=ws_id, unit=unit, conn=conn
    )
    if effective_viewer:
        updated_rows = await _filter_rows_by_viewer_web_read(
            updated_rows,
            current_workspace_id=ws_id,
            viewer_user_id=effective_viewer,
            check_current_workspace=True,
        )
    status_payload = _derive_sector_status_and_details(
        workspace_id=ws_id,
        unit=unit,
        tz_name=sector["tz"],
        tz_source=sector["tz_source"],
        sector_rows=updated_rows,
    )
    status_payload["paused_by_user"] = True
    status_payload["paused_count"] = len(paused_names)
    return status_payload


async def enable_tracking(
    user_id: str,
    workspace_id: str,
    tier: str | None = None,
    *,
    force: bool = False,
    respect_user_pause: bool = True,
    override_pause_versions: Mapping[str, int] | None = None,
    viewer_user_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Idempotently create or resume all 4 standard tracking tasks for a company workspace.

    When ``respect_user_pause=True`` and ``force=False``, skips re-enabling if any
    standard task carries ``metadata.paused_by_user == True``.
    """
    from src.dalton.settings import is_coverage_units_enabled

    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    effective_viewer = (
        str(viewer_user_id).strip()
        if viewer_user_id is not None and str(viewer_user_id).strip()
        else _SYSTEM_JOB_VIEWER_USER_ID.get()
    )
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    prelock_tok = (
        _prelock_unit_workspace.set(ws_id)
        if conn is None and is_coverage_units_enabled()
        else None
    )
    try:
        lock_ctx = (
            user_automations_lock(uid, conn=conn)
            if conn is not None
            else user_automations_lock(uid)
        )
        async with lock_ctx as active_conn:
            conn = active_conn
            try:
                ticker, company_rows = await _load_company_automations(uid, ws_id, conn=conn)
            except ValueError:
                sector = await _resolve_workspace_sector(ws_id, conn=conn)
                if sector is not None:
                    return await _enable_sector_tracking(
                        uid,
                        ws_id,
                        sector,
                        force=force,
                        respect_user_pause=respect_user_pause,
                        viewer_user_id=effective_viewer,
                        conn=conn,
                    )
                raise
            if effective_viewer and is_coverage_units_enabled():
                from src.dalton.sharing import require_workspace_access

                await require_workspace_access(
                    ws_id, effective_viewer, channel="web", need="read"
                )
            resolved_tier = await resolve_default_tier(ticker, tier, user_id=uid, conn=conn)

            unit_id_for_specs: str | None = None
            active_co_unit: dict[str, Any] | None = None
            if is_coverage_units_enabled():
                from src.dalton.database.coverage_units import get_active_unit_by_workspace

                active_co_unit = await get_active_unit_by_workspace(
                    ws_id, conn=conn, for_share=True
                )
                if (
                    active_co_unit is not None
                    and str(active_co_unit.get("kind") or "").strip().lower() == "company"
                ):
                    unit_id_for_specs = str(active_co_unit.get("unit_id") or "").strip() or None
                    all_unit_candidates = await list_all_user_automations(uid, conn=conn)
                    company_rows = _merge_unit_managed_rows(
                        company_rows,
                        candidates=all_unit_candidates,
                        workspace_id=ws_id,
                        unit=active_co_unit,
                    )

            raw_current = _derive_status_and_details(
                workspace_id=ws_id,
                ticker=ticker,
                tier=resolved_tier,
                company_rows=company_rows,
            )
            specs = build_company_watch_specs(
                ticker=ticker,
                workspace_id=ws_id,
                tier=resolved_tier,
                unit_id=unit_id_for_specs,
            )

            # Item 10 / r4 Decision 3: when tier is radar or produces no standard specs, NEVER resume any task
            # (even if force=True); pause active standard tasks AND unstarted earnings once tasks by policy.
            if not specs:
                paused_ids_empty: list[str] = []
                t_lower_empty = str(ticker).strip().lower()
                t_slug_empty = t_lower_empty.replace(".", "-")
                std_prefixes_empty = tuple(
                    dict.fromkeys((f"dalton-{t_lower_empty}-", f"dalton-{t_slug_empty}-"))
                )
                std_suffixes_empty = ("watch", "delta", "move-up", "move-down")
                for r in company_rows:
                    r_name = str(r.get("name") or "").strip().lower()
                    auto_id = str(r.get("automation_id") or "").strip()
                    r_status = str(r.get("status") or "active").strip().lower()
                    if not auto_id or r_status in ("disabled", "completed", "deleted"):
                        continue
                    is_std_row = any(
                        r_name == f"{p}{sfx}"
                        for p in std_prefixes_empty
                        for sfx in std_suffixes_empty
                    )
                    is_unstarted_earn_once = (
                        any(r_name.startswith(f"{p}earn-") for p in std_prefixes_empty)
                        and str(r.get("trigger_type") or "").strip().lower() == "once"
                        and r.get("last_run_at") is None
                        and not (isinstance(r.get("run_count"), int) and r["run_count"] > 0)
                    )
                    if not (is_std_row or is_unstarted_earn_once):
                        continue
                    if r_status != "active":
                        continue
                    new_meta = (
                        dict(r.get("metadata") or {})
                        if isinstance(r.get("metadata"), Mapping)
                        else {}
                    )
                    new_meta["paused_by_policy"] = True
                    new_meta["tier"] = resolved_tier
                    await auto_db.update_automation(
                        auto_id,
                        uid,
                        status="paused",
                        next_run_at=None,
                        metadata=new_meta,
                        conn=conn,
                    )
                    paused_ids_empty.append(auto_id)
                if paused_ids_empty:
                    from src.server.database import automation_executions as exec_db

                    await exec_db.skip_unstarted_executions_for_automations(
                        paused_ids_empty,
                        reason="watchlist_policy",
                        error_message="Skipped unstarted execution because tier policy has no enabled tracking tasks.",
                        conn=conn,
                    )
                _, refreshed_company_empty = await _load_company_automations(
                    uid, ws_id, conn=conn
                )
                if active_co_unit is not None and unit_id_for_specs:
                    all_unit_after = await list_all_user_automations(uid, conn=conn)
                    refreshed_company_empty = _merge_unit_managed_rows(
                        refreshed_company_empty,
                        candidates=all_unit_after,
                        workspace_id=ws_id,
                        unit=active_co_unit,
                    )
                visible_empty = (
                    await _filter_rows_by_viewer_web_read(
                        refreshed_company_empty,
                        current_workspace_id=ws_id,
                        viewer_user_id=effective_viewer,
                        check_current_workspace=True,
                    )
                    if effective_viewer and is_coverage_units_enabled()
                    else refreshed_company_empty
                )
                current_empty = _derive_status_and_details(
                    workspace_id=ws_id,
                    ticker=ticker,
                    tier=resolved_tier,
                    company_rows=visible_empty,
                )
                return {
                    **current_empty,
                    "skipped": True,
                    "skip_reason": "empty_tier_policy",
                }

            from src.dalton.watchlist.pause_guards import (
                build_locked_resume_reconciler,
                can_override_user_pause_locked,
            )

            # r4 Decision 3: overriding paused_by_user requires explicit config_version authorization
            # (override_pause_versions); missing version authorization defaults to refusing override.
            has_unauthorized_user_pause = any(
                isinstance(r, Mapping)
                and isinstance(r.get("metadata"), Mapping)
                and bool(r["metadata"].get("paused_by_user"))
                and not can_override_user_pause_locked(
                    r,
                    override_user_pause=bool(force),
                    override_pause_versions=override_pause_versions,
                )
                for r in company_rows
            )
            if respect_user_pause and raw_current.get("paused_by_user") and has_unauthorized_user_pause:
                visible_rows = (
                    await _filter_rows_by_viewer_web_read(
                        company_rows,
                        current_workspace_id=ws_id,
                        viewer_user_id=effective_viewer,
                        check_current_workspace=True,
                    )
                    if effective_viewer and is_coverage_units_enabled()
                    else company_rows
                )
                current = _derive_status_and_details(
                    workspace_id=ws_id,
                    ticker=ticker,
                    tier=resolved_tier,
                    company_rows=visible_rows,
                )
                return {
                    **current,
                    "skipped": True,
                    "skip_reason": "paused_by_user",
                }

            delta_cron, delta_tz = resolve_delta_schedule(resolved_tier)
            expected_names = {str(s["name"]) for s in specs}
            outer_ctx = get_current_history_context()
            effective_actor = (
                str(outer_ctx.actor_user_id).strip()
                if outer_ctx is not None
                and outer_ctx.has_explicit_actor
                and outer_ctx.actor_user_id
                else uid
            )
            by_name_company = {
                str(r.get("name") or "").strip(): r
                for r in company_rows
                if isinstance(r, dict) and r.get("name")
            }
            delta_name = f"dalton-{str(ticker).strip().lower()}-delta"
            # One marked row is enough. A later unmarked duplicate must not be upserted.
            delta_replaced = any(
                isinstance(r, dict)
                and str(r.get("name") or "").strip() == delta_name
                and _replaced_by_tracker(r)
                for r in company_rows
            )

            locked_std_reconciler = build_locked_resume_reconciler(
                override_user_pause=bool(force),
                override_pause_versions=override_pause_versions,
                skip_policy_pause=False,
            )

            for spec in specs:
                s_name = str(spec.get("name") or "")
                existing_c = by_name_company.get(s_name)
                if s_name == delta_name and delta_replaced:
                    continue
                if existing_c and _replaced_by_tracker(existing_c):
                    continue
                if existing_c:
                    ex_meta = (
                        existing_c.get("metadata")
                        if isinstance(existing_c.get("metadata"), Mapping)
                        else {}
                    )
                    if str(existing_c.get("status") or "").strip().lower() == "disabled":
                        continue
                    if bool(str(ex_meta.get("handoff_paused_reason") or "").strip()):
                        continue
                    if respect_user_pause and bool(ex_meta.get("paused_by_user")):
                        if not can_override_user_pause_locked(
                            existing_c,
                            override_user_pause=bool(force),
                            override_pause_versions=override_pause_versions,
                        ):
                            continue
                spec_copy = dict(spec)
                spec_meta = dict(spec_copy.get("metadata") or {})
                spec_meta["paused_by_user"] = None
                spec_meta["paused_by_policy"] = None
                spec_meta["tier"] = resolved_tier
                if str(spec_copy.get("name") or "") == delta_name:
                    spec_copy["cron_expression"] = delta_cron
                    spec_copy["timezone"] = delta_tz
                spec_copy["metadata"] = spec_meta
                spec_copy["status"] = "active"
                s_name = str(spec_copy["name"])
                was_paused = bool(
                    existing_c and str(existing_c.get("status") or "").strip().lower() == "paused"
                )
                with automation_history_context(
                    actor_user_id=effective_actor,
                    action="resume" if was_paused else "update",
                    skip_if_unchanged=True,
                    locked_row_reconciler=locked_std_reconciler,
                ):
                    await schedule_dalton_automation_helper(
                        s_name,
                        spec_copy,
                        user_id=uid,
                        workspace_id=ws_id,
                        ticker=ticker,
                        role="chief_of_staff",
                        action="upsert",
                        conn=conn,
                    )

            # Also resume paused non-standard unit-managed tasks via the shared schedule helper,
            # ALWAYS skipping policy-paused rows (`paused_by_policy`) and verifying user pause under lock.
            locked_nonstd_reconciler = build_locked_resume_reconciler(
                override_user_pause=bool(force),
                override_pause_versions=override_pause_versions,
                skip_policy_pause=True,
            )
            resume_refusals: list[dict[str, str]] = []
            for r in company_rows:
                r_name = str(r.get("name") or "").strip()
                if r_name in expected_names:
                    continue
                if _replaced_by_tracker(r):
                    continue
                r_meta = r.get("metadata") if isinstance(r.get("metadata"), Mapping) else {}
                if bool(str(r_meta.get("handoff_paused_reason") or "").strip()):
                    continue
                if bool(r_meta.get("paused_by_policy")):
                    continue
                if respect_user_pause and bool(r_meta.get("paused_by_user")):
                    if not can_override_user_pause_locked(
                        r,
                        override_user_pause=bool(force),
                        override_pause_versions=override_pause_versions,
                    ):
                        continue
                if str(r.get("status") or "").lower() == "paused" and r.get("automation_id"):
                    resume_spec: dict[str, Any] = {
                        "action": "resume",
                        "automation_id": str(r["automation_id"]),
                    }
                    with automation_history_context(
                        actor_user_id=effective_actor,
                        action="resume",
                        skip_if_unchanged=True,
                        locked_row_reconciler=locked_nonstd_reconciler,
                    ):
                        resume_res = await schedule_dalton_automation_helper(
                            r_name,
                            resume_spec,
                            user_id=uid,
                            workspace_id=ws_id,
                            ticker=ticker,
                            role="chief_of_staff",
                            action="resume",
                            actor_user_id=effective_actor,
                            conn=conn,
                        )
                    if not resume_res.get("ok"):
                        resume_refusals.append(
                            {
                                "automation_id": str(r["automation_id"]),
                                "name": r_name,
                                "error": str(resume_res.get("error") or "resume_refused"),
                                "message": str(
                                    resume_res.get("message")
                                    or resume_res.get("error")
                                    or ""
                                ),
                            }
                        )

            _, updated_rows = await _load_company_automations(
                uid, ws_id, ticker=ticker, unit=active_co_unit, conn=conn
            )
            if effective_viewer and is_coverage_units_enabled():
                updated_rows = await _filter_rows_by_viewer_web_read(
                    updated_rows,
                    current_workspace_id=ws_id,
                    viewer_user_id=effective_viewer,
                    check_current_workspace=True,
                )
            payload = _derive_status_and_details(
                workspace_id=ws_id,
                ticker=ticker,
                tier=resolved_tier,
                company_rows=updated_rows,
            )
            if resume_refusals:
                payload["resume_refusals"] = resume_refusals
            return payload
    finally:
        if prelock_tok is not None:
            _prelock_unit_workspace.reset(prelock_tok)


async def disable_tracking(
    user_id: str,
    workspace_id: str,
    *,
    viewer_user_id: str | None = None,
) -> dict[str, Any]:
    """Pause all ``dalton-<t>-*`` tasks (including ``-earn-*``) for a company workspace without deleting them.

    If standard tasks do not exist yet, creates them in ``paused`` state with
    ``metadata.paused_by_user = True`` so coverage completion will not auto-open tracking.
    """
    from src.dalton.settings import is_coverage_units_enabled

    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    effective_viewer = (
        str(viewer_user_id).strip()
        if viewer_user_id is not None and str(viewer_user_id).strip()
        else _SYSTEM_JOB_VIEWER_USER_ID.get()
    )
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    prelock_tok = (
        _prelock_unit_workspace.set(ws_id)
        if is_coverage_units_enabled()
        else None
    )
    paused_ids: list[str] = []
    pause_cutoff: datetime | None = None
    result_payload: dict[str, Any] | None = None
    try:
        async with user_automations_lock(uid) as conn:
            try:
                ticker, company_rows = await _load_company_automations(uid, ws_id, conn=conn)
            except ValueError:
                sector = await _resolve_workspace_sector(ws_id, conn=conn)
                if sector is not None:
                    result_payload = await _disable_sector_tracking(
                        uid,
                        ws_id,
                        sector,
                        viewer_user_id=effective_viewer,
                        paused_ids_out=paused_ids,
                        conn=conn,
                    )
                else:
                    raise
            if result_payload is None:
                if effective_viewer and is_coverage_units_enabled():
                    from src.dalton.sharing import require_workspace_access

                    await require_workspace_access(
                        ws_id, effective_viewer, channel="web", need="read"
                    )
                resolved_tier = await resolve_default_tier(ticker, user_id=uid, conn=conn)
                unit_id_for_specs: str | None = None
                active_co_unit: dict[str, Any] | None = None
                if is_coverage_units_enabled():
                    from src.dalton.database.coverage_units import get_active_unit_by_workspace

                    active_co_unit = await get_active_unit_by_workspace(
                        ws_id, conn=conn, for_share=True
                    )
                    if (
                        active_co_unit is not None
                        and str(active_co_unit.get("kind") or "").strip().lower() == "company"
                    ):
                        unit_id_for_specs = str(active_co_unit.get("unit_id") or "").strip() or None
                        all_unit_candidates = await list_all_user_automations(uid, conn=conn)
                        company_rows = _merge_unit_managed_rows(
                            company_rows,
                            candidates=all_unit_candidates,
                            workspace_id=ws_id,
                            unit=active_co_unit,
                        )
                specs = build_company_watch_specs(
                    ticker=ticker,
                    workspace_id=ws_id,
                    tier=resolved_tier,
                    unit_id=unit_id_for_specs,
                )
                by_name = {
                    str(r.get("name") or "").strip(): r
                    for r in company_rows
                    if isinstance(r, dict) and r.get("name")
                }

                paused_names: list[str] = []
                conn_kw = {"conn": conn} if conn is not None else {}
                outer_ctx = get_current_history_context()
                effective_actor = (
                    str(outer_ctx.actor_user_id).strip()
                    if outer_ctx is not None
                    and outer_ctx.has_explicit_actor
                    and outer_ctx.actor_user_id
                    else uid
                )
                with automation_history_context(
                    actor_user_id=effective_actor,
                    action="pause",
                    skip_if_unchanged=True,
                ):
                    for r in company_rows:
                        auto_id = r.get("automation_id")
                        if not auto_id:
                            continue
                        new_meta = dict(r.get("metadata") or {})
                        new_meta["paused_by_user"] = True
                        pause_kwargs: dict[str, Any] = {
                            "status": "paused",
                            "metadata": new_meta,
                        }
                        if str(r.get("trigger_type") or "").strip().lower() != "once":
                            pause_kwargs["next_run_at"] = None
                        await auto_db.update_automation(
                            str(auto_id),
                            uid,
                            **pause_kwargs,
                            **conn_kw,
                        )
                        paused_names.append(str(r.get("name") or auto_id))
                        paused_ids.append(str(auto_id))

                delta_cron, delta_tz = resolve_delta_schedule(resolved_tier)
                with automation_history_context(
                    actor_user_id=effective_actor,
                    action="create",
                    skip_if_unchanged=True,
                ):
                    for spec in specs:
                        s_name = str(spec["name"])
                        if s_name in by_name:
                            continue
                        spec_copy = dict(spec)
                        spec_meta = dict(spec_copy.get("metadata") or {})
                        spec_meta["paused_by_user"] = True
                        spec_meta["tier"] = resolved_tier
                        if s_name.endswith("-delta"):
                            spec_copy["cron_expression"] = delta_cron
                            spec_copy["timezone"] = delta_tz
                        spec_copy["metadata"] = spec_meta
                        spec_copy["status"] = "paused"
                        await schedule_dalton_automation_helper(
                            s_name,
                            spec_copy,
                            user_id=uid,
                            workspace_id=ws_id,
                            ticker=ticker,
                            role="chief_of_staff",
                            action="upsert",
                            conn=conn,
                        )
                        paused_names.append(s_name)

                _, updated_rows = await _load_company_automations(
                    uid, ws_id, ticker=ticker, unit=active_co_unit, conn=conn
                )
                if effective_viewer and is_coverage_units_enabled():
                    updated_rows = await _filter_rows_by_viewer_web_read(
                        updated_rows,
                        current_workspace_id=ws_id,
                        viewer_user_id=effective_viewer,
                        check_current_workspace=True,
                    )
                status_payload = _derive_status_and_details(
                    workspace_id=ws_id,
                    ticker=ticker,
                    tier=resolved_tier,
                    company_rows=updated_rows,
                )
                status_payload["paused_by_user"] = True
                status_payload["paused_count"] = len(paused_names)
                result_payload = status_payload

            if paused_ids:
                from src.server.database import automation_executions as exec_db

                pause_cutoff = await exec_db.get_db_now(conn=conn)
    finally:
        if prelock_tok is not None:
            _prelock_unit_workspace.reset(prelock_tok)

    if paused_ids:
        from src.server.database import automation_executions as exec_db

        skipped_execs = await exec_db.skip_unstarted_executions_for_automations(
            paused_ids,
            reason="disabled",
            error_message="Skipped unstarted execution because tracking was disabled.",
            created_at_lte=pause_cutoff,
        )
        if result_payload is not None and skipped_execs:
            result_payload["skipped_executions"] = skipped_execs

    return result_payload


def _iso_or_none(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return str(val.isoformat())
    return str(val)


async def resync_delta_schedule(
    user_id: str,
    ticker: str,
    *,
    tier: str | None = None,
) -> dict[str, Any]:
    """Rewrite a company's ``dalton-<t>-delta`` cron, timezone, and ``next_run_at`` for its current tier.

    Does nothing when no delta task exists (tracking not enabled). Preserves task
    ``status`` and ``metadata.paused_by_user`` when the task was paused by the user.
    """
    uid = str(user_id or "").strip()
    t_upper = str(ticker or "").strip().upper()
    if not uid:
        raise ValueError("user_id is required")
    if not t_upper:
        raise ValueError("ticker is required")

    delta_name = f"dalton-{t_upper.lower()}-delta"

    async with user_automations_lock(uid) as conn:
        resolved_tier = await resolve_default_tier(t_upper, tier, user_id=uid, conn=conn)
        from src.dalton.watchlist.policies import get_default_tier_policy

        tier_policy = get_default_tier_policy(resolved_tier)
        new_cron, new_tz = resolve_delta_schedule(resolved_tier)

        rows = await list_all_user_automations(
            uid,
            name_prefix=delta_name,
            conn=conn,
        )
        delta_rows = [
            r
            for r in rows
            if isinstance(r, dict)
            and str(r.get("name") or "").strip().lower() == delta_name
            and r.get("automation_id")
        ]
        if not delta_rows:
            return {
                "ok": True,
                "updated": False,
                "reason": "no_delta_task",
                "user_id": uid,
                "ticker": t_upper,
                "tier": resolved_tier,
                "name": delta_name,
            }
        open_rows = [row for row in delta_rows if not _replaced_by_tracker(row)]
        if not open_rows:
            return {
                "ok": True,
                "updated": False,
                "reason": "replaced_by_tracker",
                "user_id": uid,
                "ticker": t_upper,
                "tier": resolved_tier,
                "name": delta_name,
            }

        from src.server.services.automation_scheduler import AutomationScheduler

        if tier_policy.delta_enabled:
            new_next_run_at = AutomationScheduler.calculate_first_run(new_cron, new_tz)
        else:
            new_next_run_at = None
        conn_kw = {"conn": conn} if conn is not None else {}

        outer_ctx = get_current_history_context()
        effective_actor = (
            str(outer_ctx.actor_user_id).strip()
            if outer_ctx is not None
            and outer_ctx.has_explicit_actor
            and outer_ctx.actor_user_id
            else uid
        )
        changes: list[dict[str, Any]] = []
        paused_ids_for_skip: list[str] = []
        with automation_history_context(
            actor_user_id=effective_actor,
            action="pause" if not tier_policy.delta_enabled else "update",
            skip_if_unchanged=True,
        ):
            for row in open_rows:
                auto_id = str(row["automation_id"])
                old_cron = row.get("cron_expression")
                old_tz = row.get("timezone")
                old_next_run = _iso_or_none(row.get("next_run_at"))
                old_meta = (
                    dict(row.get("metadata") or {})
                    if isinstance(row.get("metadata"), dict)
                    else {}
                )
                old_tier = old_meta.get("tier")
                new_meta = dict(old_meta)
                new_meta["ticker"] = t_upper
                new_meta["tier"] = resolved_tier

                update_kwargs: dict[str, Any] = {
                    "cron_expression": new_cron,
                    "timezone": new_tz,
                    "metadata": new_meta,
                }
                if not tier_policy.delta_enabled:
                    new_meta["paused_by_policy"] = True
                    update_kwargs["status"] = "paused"
                    update_kwargs["next_run_at"] = None
                    paused_ids_for_skip.append(auto_id)
                else:
                    row_status = str(row.get("status") or "active").strip().lower()
                    has_blocker = bool(
                        new_meta.get("paused_by_user")
                        or new_meta.get("handoff_paused_reason")
                    )
                    if row_status == "paused" and new_meta.get("paused_by_policy") and not has_blocker:
                        new_meta.pop("paused_by_policy", None)
                        update_kwargs["status"] = "active"
                        update_kwargs["next_run_at"] = new_next_run_at
                    elif row_status == "paused":
                        # Item 8: paused automations ALWAYS have next_run_at=None
                        update_kwargs["next_run_at"] = None
                    else:
                        new_meta.pop("paused_by_policy", None)
                        update_kwargs["next_run_at"] = new_next_run_at

                updated_row = await auto_db.update_automation(
                    auto_id,
                    uid,
                    **update_kwargs,
                    **conn_kw,
                )
                effective_row = updated_row if isinstance(updated_row, dict) else row
                effective_status = str(
                    effective_row.get("status") or update_kwargs.get("status") or row.get("status") or "active"
                )
                changes.append(
                    {
                        "automation_id": auto_id,
                        "name": delta_name,
                        "status": effective_status,
                        "paused_by_user": bool(new_meta.get("paused_by_user")),
                        "old_tier": old_tier,
                        "new_tier": resolved_tier,
                        "old_cron_expression": old_cron,
                        "new_cron_expression": new_cron,
                        "old_timezone": old_tz,
                        "new_timezone": new_tz,
                        "old_next_run_at": old_next_run,
                        "new_next_run_at": _iso_or_none(update_kwargs.get("next_run_at")),
                    }
                )

        if paused_ids_for_skip and conn is not None:
            from src.server.database import automation_executions as exec_db

            await exec_db.skip_unstarted_executions_for_automations(
                paused_ids_for_skip,
                reason="watchlist_policy",
                error_message="Skipped unstarted execution because tier policy disabled delta.",
                conn=conn,
            )

        primary = changes[0]
        return {
            "ok": True,
            "updated": True,
            "user_id": uid,
            "ticker": t_upper,
            "tier": resolved_tier,
            "automation_id": primary["automation_id"],
            "name": primary["name"],
            "status": primary["status"],
            "paused_by_user": primary["paused_by_user"],
            "old_tier": primary["old_tier"],
            "new_tier": primary["new_tier"],
            "old_cron_expression": primary["old_cron_expression"],
            "new_cron_expression": primary["new_cron_expression"],
            "cron_expression": primary["new_cron_expression"],
            "old_timezone": primary["old_timezone"],
            "new_timezone": primary["new_timezone"],
            "timezone": primary["new_timezone"],
            "old_next_run_at": primary["old_next_run_at"],
            "new_next_run_at": primary["new_next_run_at"],
            "next_run_at": primary["new_next_run_at"],
            "changes": changes,
        }


async def set_tier_and_resync(
    user_id: str,
    tickers: Sequence[str],
    tier: str,
) -> dict[str, Any]:
    """Change tickers' coverage tier via the shared WatchlistMutationService (same-tx Outbox + reconciler)."""
    from src.dalton.watchlist.service import WatchlistMutationService

    res = await WatchlistMutationService().execute_mutation(
        user_id,
        action="set_tier",
        tickers=list(tickers),
        tier=tier,
    )
    res.setdefault("delta_schedule", [])
    return res


def _filter_unit_managed_rows(
    rows: Sequence[Any],
    *,
    clean_ws: str,
    clean_unit_id: str,
    eff_unit: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    from src.dalton.autonomy.classification import is_managed_unit_automation_for_unit

    return [
        dict(r)
        for r in rows
        if isinstance(r, Mapping)
        and is_managed_unit_automation_for_unit(r, unit=eff_unit)
        and (
            (
                bool(clean_ws)
                and (
                    str(r.get("workspace_id") or "").strip() == clean_ws
                    or (
                        clean_unit_id
                        and not str(r.get("workspace_id") or "").strip()
                        and str(
                            (
                                (r.get("metadata") or {})
                                if isinstance(r.get("metadata"), Mapping)
                                else {}
                            ).get("unit_id")
                            or ""
                        ).strip()
                        == clean_unit_id
                    )
                )
            )
            or (
                not clean_ws
                and clean_unit_id
                and str(
                    (
                        (r.get("metadata") or {})
                        if isinstance(r.get("metadata"), Mapping)
                        else {}
                    ).get("unit_id")
                    or ""
                ).strip()
                == clean_unit_id
            )
        )
    ]
