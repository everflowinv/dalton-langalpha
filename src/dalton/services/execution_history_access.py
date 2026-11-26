"""Per-page Python visibility resolution for automation execution history links.

Replaces SQL-level ``t.user_id = a.user_id`` thread joins so:
- Upstream SQL queries never reference Dalton tables or columns.
- Linked thread and colleague workspace accessibility follow the exact same
  web read rules as ``src.dalton.sharing.require_thread_access`` and
  ``src.dalton.sharing.require_workspace_access`` (respecting colleague sharing
  capability, colleague dispatch toggle, team membership, external-member
  isolation, foreign Home/stand-in guards, workspace sharing level, and
  deleted/unbound workspace status).
- Inaccessible links are stripped (``conversation_thread_id = None`` and, when
  no accessible fallback workspace applies, ``workspace_id = None``), keeping
  the execution record itself intact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton import sharing as sharing_mod
from src.server.database import conversation as conv_db
from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)


def _coerce_config_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


async def _load_workspace_for_access(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Load the minimal live workspace row needed by ``require_workspace_access``."""
    ws_id = normalize_uuid(workspace_id)
    if ws_id is None:
        return None
    try:
        async with get_db_connection(conn) as owned:
            async with owned.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT workspace_id, user_id, status, config
                    FROM workspaces
                    WHERE workspace_id = %s
                      AND status <> 'deleted'
                    """,
                    (ws_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                ws = dict(row)
                ws["config"] = _coerce_config_dict(ws.get("config"))
                return ws
    except Exception as exc:
        logger.warning(
            "Execution history workspace lookup failed (%s); hiding workspace link",
            type(exc).__name__,
        )
        return None


async def _resolve_thread_workspace_for_caller(
    thread_id: str,
    user_id: str,
    *,
    conn: Any = None,
) -> str | None:
    """Return the thread's live ``workspace_id`` if ``user_id`` can read it on web, else None."""
    if not sharing_mod._sharing_auth_enabled():
        # Without Dalton colleague sharing, only threads in live caller-owned
        # workspaces are readable, and the SQL join already matched those.
        return None
    try:
        meta = await conv_db.get_thread_auth_meta(thread_id, conn=conn)
    except Exception as exc:
        logger.warning(
            "Execution history thread metadata lookup failed (%s); hiding thread link",
            type(exc).__name__,
        )
        return None
    if not meta:
        return None

    ws_status = str(meta.get("workspace_status") or "").strip().lower()
    if ws_status in ("deleted", "unbound"):
        return None

    ws_id = str(meta.get("workspace_id") or "").strip()
    if not ws_id:
        return None

    ws_owner = str(meta.get("workspace_owner_id") or "").strip()
    thread_owner = str(
        meta.get("thread_user_id") or meta.get("user_id") or ""
    ).strip()
    ws_dict = {
        "workspace_id": ws_id,
        "user_id": ws_owner,
        "status": meta.get("workspace_status"),
        "config": _coerce_config_dict(meta.get("workspace_config")),
    }
    try:
        await sharing_mod.require_thread_access(
            thread_id,
            user_id,
            channel="web",
            need="read",
            conn=conn,
            owner_id=thread_owner or None,
            workspace_owner_id=ws_owner or None,
            workspace_id=ws_dict,
        )
    except (HTTPException, PermissionError):
        return None
    return ws_id


async def _resolve_workspace_for_caller(
    workspace_id: str,
    user_id: str,
    *,
    conn: Any = None,
) -> str | None:
    """Return ``workspace_id`` if live and readable by ``user_id`` on web, else None."""
    if not sharing_mod._sharing_auth_enabled():
        # Without Dalton colleague sharing, only caller-owned live workspaces
        # (already flagged by _task_workspace_owned in SQL) are readable.
        return None
    ws = await _load_workspace_for_access(workspace_id, conn=conn)
    if not ws:
        return None
    ws_status = str(ws.get("status") or "").strip().lower()
    if ws_status in ("deleted", "unbound"):
        return None
    try:
        await sharing_mod.require_workspace_access(
            workspace_id,
            user_id,
            channel="web",
            need="read",
            workspace=ws,
            conn=conn,
        )
    except (HTTPException, PermissionError):
        return None
    return str(ws["workspace_id"])


async def resolve_execution_history_page_links(
    user_id: str,
    rows: list[dict[str, Any]],
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Resolve linked thread & workspace accessibility per execution history page.

    Expects rows from ``list_executions`` carrying upstream-only helper columns
    ``_owned_thread_workspace_id``, ``_task_workspace_id``, and
    ``_task_workspace_owned`` (all stripped before returning). Rows already
    matched by the owner-only live-workspace SQL join incur zero extra queries.
    """
    if not rows:
        return rows

    thread_cache: dict[str, str | None] = {}
    workspace_cache: dict[str, str | None] = {}

    async def _resolve_fallback_ws(
        task_ws: Any, task_ws_owned: bool
    ) -> Any:
        if task_ws is None:
            return None
        if task_ws_owned:
            return task_ws
        ws_key = str(task_ws).strip()
        if not ws_key:
            return None
        if ws_key not in workspace_cache:
            workspace_cache[ws_key] = await _resolve_workspace_for_caller(
                ws_key, user_id, conn=conn
            )
        return workspace_cache[ws_key]

    for row in rows:
        has_sql_markers = (
            "_owned_thread_workspace_id" in row
            or "_task_workspace_id" in row
            or "_task_workspace_owned" in row
        )
        owned_thread_ws = row.pop("_owned_thread_workspace_id", None)
        task_ws = row.pop("_task_workspace_id", None)
        task_ws_owned = bool(row.pop("_task_workspace_owned", False))

        if not has_sql_markers:
            continue

        raw_thread_id = row.get("conversation_thread_id")
        tid = str(raw_thread_id).strip() if raw_thread_id is not None else ""

        if tid:
            if owned_thread_ws is not None:
                row["workspace_id"] = owned_thread_ws
                continue
            if tid not in thread_cache:
                thread_cache[tid] = await _resolve_thread_workspace_for_caller(
                    tid, user_id, conn=conn
                )
            resolved_ws = thread_cache[tid]
            if resolved_ws is not None:
                row["workspace_id"] = resolved_ws
            else:
                row["conversation_thread_id"] = None
                row["workspace_id"] = await _resolve_fallback_ws(
                    task_ws, task_ws_owned
                )
        else:
            row["workspace_id"] = await _resolve_fallback_ws(
                task_ws, task_ws_owned
            )

    return rows
