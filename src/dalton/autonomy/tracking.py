# ruff: noqa: BLE001, S110
"""Company workspace auto-tracking status, enable, and disable service."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from src.dalton.autonomy.schedule import (
    build_company_watch_specs,
    build_sector_watch_specs,
    list_all_user_automations,
    resolve_delta_schedule,
    schedule_dalton_automation_helper,
    user_automations_lock,
)
from src.dalton.coverage import get_user_tier, set_user_coverage_tier
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


async def resolve_workspace_id_by_ticker(ticker: str, user_id: str) -> str | None:
    """Resolve workspace_id for ``ticker`` and ``user_id`` (following ``bootstrap_watch._resolve_workspace_id``)."""
    t_upper = str(ticker or "").strip().upper()
    uid = str(user_id or "").strip()
    if not t_upper or not uid:
        return None
    try:
        async with get_db_connection() as conn, conn.cursor() as cur:
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


async def _resolve_workspace_ticker(
    workspace_id: str,
    existing_rows: list[dict[str, Any]] | None = None,
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

    by_name: dict[str, dict[str, Any]] = {}
    for r in company_rows:
        r_name = str(r.get("name") or "").strip()
        if r_name and r_name not in by_name:
            by_name[r_name] = r

    existing_standard = [by_name[n] for n in expected_names if n in by_name]
    missing_tasks = [n for n in expected_names if n not in by_name]
    active_tasks = [
        n
        for n in expected_names
        if n in by_name and str(by_name[n].get("status") or "").lower() == "active"
    ]
    paused_tasks = [
        n
        for n in expected_names
        if n in by_name and str(by_name[n].get("status") or "").lower() == "paused"
    ]

    total_expected = len(expected_names)
    if len(existing_standard) == total_expected and len(active_tasks) == total_expected:
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
        isinstance(r.get("metadata"), dict) and bool(r["metadata"].get("paused_by_user"))
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

    return {
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


async def _load_company_automations(
    user_id: str,
    workspace_id: str,
    *,
    ticker: str | None = None,
    conn: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve ``ticker`` and load all ``dalton-<ticker>-*`` automation rows for ``(user_id, workspace_id)``."""
    resolved_ticker = ticker
    if not resolved_ticker:
        try:
            resolved_ticker = await _resolve_workspace_ticker(workspace_id)
        except ValueError:
            all_rows = await list_all_user_automations(
                user_id,
                name_prefix="dalton-",
                conn=conn,
            )
            resolved_ticker = await _resolve_workspace_ticker(
                workspace_id,
                existing_rows=all_rows,
            )
            return resolved_ticker, _filter_company_automations(
                all_rows,
                workspace_id=workspace_id,
                ticker=resolved_ticker,
            )

    prefix = f"dalton-{resolved_ticker.lower()}-"
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
        user_id=str(unit.get("owner_user_id") or ""),
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
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Load every automation row owned by this sector unit (any workspace)."""
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
    rows = await _load_sector_automations(uid, unit_id, conn=conn)
    paused: list[str] = []
    conn_kw = {"conn": conn} if conn is not None else {}
    for r in rows:
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


async def get_tracking_status(user_id: str, workspace_id: str) -> dict[str, Any]:
    """Derive the company workspace tracking status (`enabled` | `disabled` | `incomplete` | `not_applicable`) from `automations`."""
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    try:
        ticker, company_rows = await _load_company_automations(uid, ws_id)
    except ValueError:
        # 公司身份解析失败：已登记行业单元（开关打开时）给出行业跟踪状态；
        # 其余保持 not_applicable。开关关闭时 _resolve_workspace_sector 不查
        # 注册表，老工作区的行为与 P3 前一致。
        sector = await _resolve_workspace_sector(ws_id)
        if sector is not None:
            sector_rows = await _load_sector_automations(
                uid, str(sector["unit"].get("unit_id") or "")
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
    tier = await resolve_default_tier(ticker, user_id=uid)
    return _derive_status_and_details(
        workspace_id=ws_id,
        ticker=ticker,
        tier=tier,
        company_rows=company_rows,
    )


async def _enable_sector_tracking(
    user_id: str,
    workspace_id: str,
    sector: dict[str, Any],
    *,
    force: bool = False,
    respect_user_pause: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Idempotently create or resume a sector unit's two standard tasks (P3).

    Ownership is the unit identity: rows this unit left on an older workspace
    are paused first (codex r2 #1), then the suite is upserted on the unit's
    current workspace. Member companies are never touched — the specs address
    `dalton-sector-<slug>-*` names, disjoint from `dalton-<ticker>-*`.
    """
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
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

    sector_rows = await _load_sector_automations(uid, unit_id, conn=conn)
    current = _derive_sector_status_and_details(
        workspace_id=ws_id,
        unit=unit,
        tz_name=sector["tz"],
        tz_source=sector["tz_source"],
        sector_rows=sector_rows,
    )
    if not force and respect_user_pause and current.get("paused_by_user"):
        return {
            **current,
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
    for spec in specs:
        spec_copy = dict(spec)
        spec_meta = dict(spec_copy.get("metadata") or {})
        spec_meta["paused_by_user"] = None
        spec_copy["metadata"] = spec_meta
        spec_copy["status"] = "active"
        await schedule_dalton_automation_helper(
            str(spec_copy["name"]),
            spec_copy,
            user_id=uid,
            workspace_id=ws_id,
            ticker=None,
            role="chief_of_staff",
            action="upsert",
            conn=conn,
        )

    updated_rows = await _load_sector_automations(uid, unit_id, conn=conn)
    result = _derive_sector_status_and_details(
        workspace_id=ws_id,
        unit=unit,
        tz_name=sector["tz"],
        tz_source=sector["tz_source"],
        sector_rows=updated_rows,
    )
    if stale_paused:
        result["paused_stale_tasks"] = stale_paused
    return result


async def _disable_sector_tracking(
    user_id: str,
    workspace_id: str,
    sector: dict[str, Any],
    *,
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
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    unit = sector["unit"]
    slug = str(unit.get("slug") or "").strip()
    unit_id = str(unit.get("unit_id") or "").strip()
    _require_valid_sector_automation_names(slug)

    sector_rows = await _load_sector_automations(uid, unit_id, conn=conn)
    by_name_current: dict[str, dict[str, Any]] = {}
    for r in sector_rows:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        row_ws = str(r.get("workspace_id") or "").strip()
        if not row_ws or row_ws == ws_id:
            by_name_current[str(r.get("name") or "").strip()] = r

    paused_names: list[str] = []
    conn_kw = {"conn": conn} if conn is not None else {}
    for r in sector_rows:
        auto_id = r.get("automation_id")
        if not auto_id:
            continue
        row_ws = str(r.get("workspace_id") or "").strip()
        new_meta = dict(r.get("metadata") or {})
        new_meta["paused_by_user"] = True
        if row_ws and row_ws != ws_id:
            new_meta["stale_unit_binding"] = True
        await auto_db.update_automation(
            str(auto_id),
            uid,
            status="paused",
            next_run_at=None,
            metadata=new_meta,
            **conn_kw,
        )
        paused_names.append(str(r.get("name") or auto_id))

    specs = build_sector_watch_specs(
        unit_id=unit_id,
        slug=slug,
        display_name=str(unit.get("display_name") or slug),
        workspace_id=ws_id,
        timezone_name=sector["tz"],
    )
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

    updated_rows = await _load_sector_automations(uid, unit_id, conn=conn)
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
) -> dict[str, Any]:
    """Idempotently create or resume all 4 standard tracking tasks for a company workspace.

    When ``respect_user_pause=True`` and ``force=False``, skips re-enabling if any
    standard task carries ``metadata.paused_by_user == True``.
    """
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    async with user_automations_lock(uid) as conn:
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
                    conn=conn,
                )
            raise
        resolved_tier = await resolve_default_tier(ticker, tier, user_id=uid, conn=conn)

        current = _derive_status_and_details(
            workspace_id=ws_id,
            ticker=ticker,
            tier=resolved_tier,
            company_rows=company_rows,
        )
        if not force and respect_user_pause and current.get("paused_by_user"):
            return {
                **current,
                "skipped": True,
                "skip_reason": "paused_by_user",
            }

        specs = build_company_watch_specs(
            ticker=ticker,
            workspace_id=ws_id,
            tier=resolved_tier,
        )
        delta_cron, delta_tz = resolve_delta_schedule(resolved_tier)
        expected_names = {str(s["name"]) for s in specs}

        for spec in specs:
            spec_copy = dict(spec)
            spec_meta = dict(spec_copy.get("metadata") or {})
            spec_meta["paused_by_user"] = None
            spec_meta["tier"] = resolved_tier
            if str(spec_copy.get("name") or "").endswith("-delta"):
                spec_copy["cron_expression"] = delta_cron
                spec_copy["timezone"] = delta_tz
            spec_copy["metadata"] = spec_meta
            spec_copy["status"] = "active"
            await schedule_dalton_automation_helper(
                str(spec_copy["name"]),
                spec_copy,
                user_id=uid,
                workspace_id=ws_id,
                ticker=ticker,
                role="chief_of_staff",
                action="upsert",
                conn=conn,
            )

        # Also resume any paused one-shot earnings tasks (`dalton-<t>-earn-*`) when re-enabling
        conn_kw = {"conn": conn} if conn is not None else {}
        for r in company_rows:
            r_name = str(r.get("name") or "").strip()
            if r_name in expected_names:
                continue
            if str(r.get("status") or "").lower() == "paused" and r.get("automation_id"):
                cleaned_meta = dict(r.get("metadata") or {})
                cleaned_meta.pop("paused_by_user", None)
                await auto_db.update_automation(
                    str(r["automation_id"]),
                    uid,
                    status="active",
                    metadata=cleaned_meta,
                    **conn_kw,
                )

        _, updated_rows = await _load_company_automations(
            uid, ws_id, ticker=ticker, conn=conn
        )
        return _derive_status_and_details(
            workspace_id=ws_id,
            ticker=ticker,
            tier=resolved_tier,
            company_rows=updated_rows,
        )


async def disable_tracking(user_id: str, workspace_id: str) -> dict[str, Any]:
    """Pause all ``dalton-<t>-*`` tasks (including ``-earn-*``) for a company workspace without deleting them.

    If standard tasks do not exist yet, creates them in ``paused`` state with
    ``metadata.paused_by_user = True`` so coverage completion will not auto-open tracking.
    """
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    async with user_automations_lock(uid) as conn:
        try:
            ticker, company_rows = await _load_company_automations(uid, ws_id, conn=conn)
        except ValueError:
            sector = await _resolve_workspace_sector(ws_id, conn=conn)
            if sector is not None:
                return await _disable_sector_tracking(uid, ws_id, sector, conn=conn)
            raise
        resolved_tier = await resolve_default_tier(ticker, user_id=uid, conn=conn)
        specs = build_company_watch_specs(
            ticker=ticker,
            workspace_id=ws_id,
            tier=resolved_tier,
        )
        by_name = {
            str(r.get("name") or "").strip(): r
            for r in company_rows
            if isinstance(r, dict) and r.get("name")
        }

        paused_names: list[str] = []
        conn_kw = {"conn": conn} if conn is not None else {}
        for r in company_rows:
            auto_id = r.get("automation_id")
            if not auto_id:
                continue
            new_meta = dict(r.get("metadata") or {})
            new_meta["paused_by_user"] = True
            await auto_db.update_automation(
                str(auto_id),
                uid,
                status="paused",
                metadata=new_meta,
                **conn_kw,
            )
            paused_names.append(str(r.get("name") or auto_id))

        delta_cron, delta_tz = resolve_delta_schedule(resolved_tier)
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
            uid, ws_id, ticker=ticker, conn=conn
        )
        status_payload = _derive_status_and_details(
            workspace_id=ws_id,
            ticker=ticker,
            tier=resolved_tier,
            company_rows=updated_rows,
        )
        status_payload["paused_by_user"] = True
        status_payload["paused_count"] = len(paused_names)
        return status_payload


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

        from src.server.services.automation_scheduler import AutomationScheduler

        new_next_run_at = AutomationScheduler.calculate_first_run(new_cron, new_tz)
        new_next_run_iso = _iso_or_none(new_next_run_at)
        conn_kw = {"conn": conn} if conn is not None else {}

        changes: list[dict[str, Any]] = []
        for row in delta_rows:
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

            updated_row = await auto_db.update_automation(
                auto_id,
                uid,
                cron_expression=new_cron,
                timezone=new_tz,
                next_run_at=new_next_run_at,
                metadata=new_meta,
                **conn_kw,
            )
            effective_row = updated_row if isinstance(updated_row, dict) else row
            effective_status = str(
                effective_row.get("status") or row.get("status") or "active"
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
                    "new_next_run_at": new_next_run_iso,
                }
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
    """Change tickers' coverage tier and move each changed company's delta task to the new schedule.

    The one entry point for a tier change (Chief of Staff's tool and the REST
    route), so both keep the daily internal-source scan on its tier's cadence.
    Only tickers the roster actually updated are resynced; a failed resync is
    reported per ticker and leaves the tier change in place.
    """
    res = await set_user_coverage_tier(user_id, tickers, tier=tier)
    resynced: list[dict[str, Any]] = []
    for t in res.get("updated_tickers") or []:
        try:
            resynced.append(await resync_delta_schedule(user_id, t, tier=res.get("tier")))
        except Exception as exc:
            logger.warning(
                "[DALTON_TRACKING] set_tier resync failed | ticker=%s error=%s",
                t,
                type(exc).__name__,
            )
            resynced.append({"ticker": t, "updated": False, "reason": "resync_failed"})
    res["delta_schedule"] = resynced
    return res
