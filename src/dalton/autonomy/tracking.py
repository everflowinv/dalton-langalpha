# ruff: noqa: BLE001, S110
"""Company workspace auto-tracking status, enable, and disable service."""

from __future__ import annotations

import logging
import re
from typing import Any

from src.dalton.autonomy.schedule import (
    build_company_watch_specs,
    list_all_user_automations,
    schedule_dalton_automation_helper,
    user_automations_lock,
)
from src.dalton.coverage import get_user_tier
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.database.home_workspace import is_home_row
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

LABEL_ENABLED_ZH = "自动跟踪：开启（盯盘每 30 分钟 · 每日增量 21:30 · 涨跌 ±4% 触发）"
LABEL_DISABLED_ZH = "自动跟踪：关闭"
LABEL_INCOMPLETE_ZH = "自动跟踪：不完整"

_TICKER_FROM_AUTO_NAME_RE = re.compile(
    r"^dalton-([a-z0-9.]+)-(?:watch|delta|move-up|move-down|earn-.*)$"
)
_CLEAN_TICKER_RE = re.compile(r"^([A-Za-z0-9]{1,10}(?:\.[A-Za-z]{1,4})?)")


async def resolve_default_tier(
    ticker: str,
    explicit_tier: str | None = None,
    *,
    user_id: str | None = None,
) -> str:
    """Resolve tier for ``ticker`` from ``user_id``'s coverage roster or default to ``'watch'``."""
    if explicit_tier and str(explicit_tier).strip():
        return str(explicit_tier).strip().lower()
    t_upper = str(ticker or "").strip().upper()
    if user_id and t_upper:
        try:
            user_tier = await get_user_tier(user_id, t_upper)
            if user_tier:
                return user_tier
        except Exception as exc:
            logger.warning("[DALTON_TRACKING] Failed resolving user tier for %s/%s: %s", user_id, t_upper, exc)
    return "watch"


def _extract_ticker_from_text(raw: str | None) -> str | None:
    cleaned = str(raw or "").strip()
    if not cleaned or cleaned.lower() == "home":
        return None
    m = _CLEAN_TICKER_RE.match(cleaned)
    if m:
        return m.group(1).upper()
    return None


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
    """Resolve the uppercase company ticker from workspace metadata, name, knowledge doc, or existing automations."""
    ws: dict[str, Any] | None = None
    try:
        ws = await ws_db.get_workspace(str(workspace_id))
    except Exception as exc:
        logger.debug("[DALTON_TRACKING] get_workspace(%s) failed: %s", workspace_id, exc)

    if ws:
        if is_home_row(ws) or bool(ws.get("is_home")):
            raise ValueError("Home workspace does not support company auto-tracking")
        meta = ws.get("metadata") if isinstance(ws.get("metadata"), dict) else {}
        meta_ticker = _extract_ticker_from_text(
            meta.get("ticker") or ws.get("ticker")
        )
        if meta_ticker:
            return meta_ticker

    try:
        from src.dalton.database import knowledge as db_knowledge

        company_row = await db_knowledge.latest_valid_version(str(workspace_id), "company")
        if company_row and isinstance(company_row.get("content"), dict):
            doc_ticker = _extract_ticker_from_text(company_row["content"].get("ticker"))
            if doc_ticker:
                return doc_ticker
    except Exception:
        pass

    if ws:
        name_ticker = _extract_ticker_from_text(ws.get("name"))
        if name_ticker:
            return name_ticker

    for r in existing_rows or []:
        if str(r.get("workspace_id") or "") and str(r.get("workspace_id")) != str(workspace_id):
            continue
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if r_meta.get("ticker"):
            return str(r_meta["ticker"]).strip().upper()
        m = _TICKER_FROM_AUTO_NAME_RE.match(str(r.get("name") or "").strip().lower())
        if m:
            return m.group(1).upper()

    raise ValueError(f"Unable to resolve company ticker for workspace '{workspace_id}'")


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


async def get_tracking_status(user_id: str, workspace_id: str) -> dict[str, Any]:
    """Derive the company workspace tracking status (`enabled` | `disabled` | `incomplete`) from `automations`."""
    uid = str(user_id or "").strip()
    ws_id = str(workspace_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    if not ws_id:
        raise ValueError("workspace_id is required")

    ticker, company_rows = await _load_company_automations(uid, ws_id)
    tier = await resolve_default_tier(ticker, user_id=uid)
    return _derive_status_and_details(
        workspace_id=ws_id,
        ticker=ticker,
        tier=tier,
        company_rows=company_rows,
    )


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
        ticker, company_rows = await _load_company_automations(uid, ws_id, conn=conn)
        resolved_tier = await resolve_default_tier(ticker, tier, user_id=uid)

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
        expected_names = {str(s["name"]) for s in specs}

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
        ticker, company_rows = await _load_company_automations(uid, ws_id, conn=conn)
        resolved_tier = await resolve_default_tier(ticker, user_id=uid)
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

        for spec in specs:
            s_name = str(spec["name"])
            if s_name in by_name:
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
