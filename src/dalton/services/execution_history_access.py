"""Per-page Python visibility resolution for automation execution history links.

Replaces SQL-level ``t.user_id = a.user_id`` thread joins so:
- Upstream SQL queries never reference Dalton tables or columns.
- Linked thread accessibility follows the exact same web read rules as
  ``src.server.database.conversation.threads_read.get_thread_auth_meta`` +
  ``src.dalton.sharing.require_thread_access`` (including preserving an
  initiator's readable thread link when a colleague workspace was soft-deleted
  while colleague dispatch is on, and blocking external members from viewing
  colleague-initiated threads inside their own workspace).
- Thread ID and workspace ID are paired: when a thread is readable, only that
  thread's own live accessible workspace ID is paired with it (or ``None`` when
  that workspace is deleted/unbound/inaccessible so the frontend resolves the
  thread directly — never substituting a migrated automation's new workspace ID).
  When a thread is absent or unreadable, ``conversation_thread_id`` is cleared
  and only an accessible task workspace ID (if any) is kept.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton import sharing as sharing_mod
from src.server.database import conversation as conv_db
from src.server.database.home_workspace import is_reserved_row
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


async def _resolve_workspace_for_caller(
    workspace_id: str,
    user_id: str,
    *,
    workspace: dict[str, Any] | None = None,
    conn: Any = None,
) -> str | None:
    """Return ``workspace_id`` if live and readable by ``user_id`` on web, else None."""
    if not sharing_mod._sharing_auth_enabled():
        # Without Dalton colleague sharing, only caller-owned live workspaces
        # (already flagged by _task_workspace_owned in SQL) are readable.
        return None
    ws = workspace if workspace is not None else await _load_workspace_for_access(
        workspace_id, conn=conn
    )
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


async def _resolve_thread_access_for_caller(
    thread_id: str,
    user_id: str,
    *,
    conn: Any = None,
) -> tuple[bool, str | None]:
    """Resolve ``(can_read_thread, accessible_workspace_id)`` using real web read rules.

    Thread readability delegates directly to ``conv_db.get_thread_auth_meta``
    and ``sharing_mod.require_thread_access(..., channel='web', need='read')``
    without prematurely rejecting soft-deleted colleague workspaces.
    Workspace ID eligibility is decided independently:
    - A deleted/unbound workspace never returns ``workspace_id`` (the caller
      still keeps ``conversation_thread_id`` when ``require_thread_access``
      permits reading their own thread).
    - Caller's own live workspace returns ``ws_id``.
    - A colleague's live workspace returns ``ws_id`` when readable via
      ``require_workspace_access`` or via the ``thread_only`` web shell rule
      (``colleague_dispatch`` enabled and caller initiated the thread in a
      live non-reserved workspace).
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False, None
    try:
        meta = await conv_db.get_thread_auth_meta(thread_id, conn=conn)
    except Exception as exc:
        logger.warning(
            "Execution history thread metadata lookup failed (%s); hiding thread link",
            type(exc).__name__,
        )
        return False, None
    if not meta:
        return False, None

    ws_id = str(meta.get("workspace_id") or "").strip()
    if not ws_id:
        return False, None

    ws_owner = str(meta.get("workspace_owner_id") or "").strip()
    thread_owner = str(
        meta.get("thread_user_id") or meta.get("user_id") or ""
    ).strip()
    ws_status = str(meta.get("workspace_status") or "").strip().lower()
    ws_dict = {
        "workspace_id": ws_id,
        "user_id": ws_owner,
        "status": meta.get("workspace_status"),
        "config": _coerce_config_dict(meta.get("workspace_config")),
    }
    try:
        await sharing_mod.require_thread_access(
            thread_id,
            uid,
            channel="web",
            need="read",
            conn=conn,
            owner_id=thread_owner or None,
            workspace_owner_id=ws_owner or None,
            workspace_id=ws_dict,
        )
    except (HTTPException, PermissionError):
        return False, None

    # Thread is readable on web; independently decide whether workspace_id is
    # returnable.
    if ws_status in ("deleted", "unbound"):
        return True, None
    if ws_owner == uid:
        return True, ws_id

    # Live colleague workspace: allow workspace_id when web workspace read is
    # granted OR when the web thread_only workspace shell rule applies
    # (colleague_dispatch enabled, caller initiated the thread, non-reserved).
    if (
        sharing_mod._sharing_auth_enabled()
        and sharing_mod.is_colleague_dispatch_enabled()
        and uid == thread_owner
        and thread_owner != ws_owner
        and not is_reserved_row(ws_dict)
    ):
        return True, ws_id

    resolved_ws = await _resolve_workspace_for_caller(
        ws_id, uid, workspace=ws_dict, conn=conn
    )
    return True, resolved_ws


async def resolve_execution_history_page_links(
    user_id: str,
    rows: list[dict[str, Any]],
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Resolve linked thread & workspace accessibility per execution history page.

    Expects rows from ``list_executions`` carrying upstream-only helper columns
    ``_owned_thread_workspace_id``, ``_task_workspace_id``, and
    ``_task_workspace_owned`` (all stripped before returning). In single-user
    OSS mode (multi-user and colleague sharing auth both off), rows already
    matched by the owner-only live-workspace SQL join incur zero extra queries.
    """
    if not rows:
        return rows

    auth_active = (
        sharing_mod._multiuser_auth_enabled()
        or sharing_mod._sharing_auth_enabled()
    )
    thread_cache: dict[str, tuple[bool, str | None]] = {}
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
            if not auth_active:
                if owned_thread_ws is not None:
                    row["workspace_id"] = owned_thread_ws
                else:
                    row["conversation_thread_id"] = None
                    row["workspace_id"] = await _resolve_fallback_ws(
                        task_ws, task_ws_owned
                    )
                continue

            if tid not in thread_cache:
                thread_cache[tid] = await _resolve_thread_access_for_caller(
                    tid, user_id, conn=conn
                )
            can_read_thread, resolved_ws = thread_cache[tid]
            if can_read_thread:
                # Pair a readable thread ONLY with its own accessible workspace
                # (or None when its workspace is gone/hidden so the frontend
                # looks up the thread directly rather than opening it under a
                # migrated automation's unrelated workspace).
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
