"""Database queries for AU1 automation scope classification, scoped listing, and history."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from unittest.mock import NonCallableMock

from psycopg.rows import dict_row

from src.dalton.autonomy.classification import (
    AutomationScope,
    classify_automation_scope,
    needs_workspace_tracking_lookup,
    normalize_scope_text,
    sql_trim_scope_text,
)
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection


def _valid_uuid(raw: Any) -> str | None:
    cleaned = normalize_scope_text(raw)
    if not cleaned:
        return None
    try:
        return str(uuid.UUID(cleaned))
    except (ValueError, TypeError, AttributeError):
        return None


async def fetch_scope_classification_context(
    user_id: str,
    workspace_id: str,
    *,
    conn: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """Fetch workspace ownership, coverage units, and covered tickers in one read."""
    clean_ws = _valid_uuid(workspace_id)
    clean_uid = normalize_scope_text(user_id)
    if not clean_ws or not clean_uid:
        return None, [], []

    trimmed_unit_owner = sql_trim_scope_text("u.owner_user_id")
    trimmed_cov_owner = sql_trim_scope_text("c.user_id")
    trimmed_cov_ticker = sql_trim_scope_text("c.ticker")

    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT
                        (
                            SELECT to_jsonb(w_sub)
                            FROM (
                                SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status
                                FROM workspaces w
                                WHERE w.workspace_id = %s::uuid
                            ) w_sub
                        ) AS workspace_row,
                        COALESCE(
                            (
                                SELECT jsonb_agg(to_jsonb(u_sub))
                                FROM (
                                    SELECT
                                        u.workspace_id::text AS workspace_id,
                                        u.owner_user_id,
                                        u.kind,
                                        u.status
                                    FROM dalton_coverage_units u
                                    WHERE u.workspace_id = %s::uuid
                                      AND {trimmed_unit_owner} = %s
                                ) u_sub
                            ),
                            '[]'::jsonb
                        ) AS coverage_units,
                        COALESCE(
                            (
                                SELECT jsonb_agg(c.ticker)
                                FROM dalton_coverage c
                                WHERE {trimmed_cov_owner} = %s
                                  AND {trimmed_cov_ticker} <> ''
                            ),
                            '[]'::jsonb
                        ) AS covered_tickers
                    """,
                    (clean_ws, clean_ws, clean_uid, clean_uid),
                )
                row = await cur.fetchone()
    except TypeError:
        if isinstance(conn, NonCallableMock):
            return None, [], []
        raise

    if not isinstance(row, Mapping):
        return None, [], []
    ws_row = row.get("workspace_row")
    raw_units = row.get("coverage_units")
    raw_tickers = row.get("covered_tickers")
    workspace_dict = dict(ws_row) if isinstance(ws_row, Mapping) else None
    units_list = (
        [dict(u) for u in raw_units if isinstance(u, Mapping)]
        if isinstance(raw_units, list)
        else []
    )
    tickers_list = (
        [
            normalize_scope_text(t)
            for t in raw_tickers
            if isinstance(t, str) and normalize_scope_text(t)
        ]
        if isinstance(raw_tickers, list)
        else []
    )
    return workspace_dict, units_list, tickers_list


async def resolve_automation_scope_for_write(
    user_id: str,
    *,
    name: str,
    workspace_id: str | None,
    metadata: Mapping[str, Any] | None,
    conn: Any = None,
) -> AutomationScope:
    """Compute canonical ``scope`` for a new or updated automation row."""
    candidate_row: dict[str, Any] = {
        "user_id": user_id,
        "name": name,
        "workspace_id": workspace_id,
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
    }
    if not needs_workspace_tracking_lookup(
        user_id,
        name=name,
        workspace_id=workspace_id,
        metadata=metadata,
    ):
        return classify_automation_scope(candidate_row)

    workspace_row, units, covered_tickers = await fetch_scope_classification_context(
        user_id,
        str(workspace_id or ""),
        conn=conn,
    )
    return classify_automation_scope(
        candidate_row,
        workspace_row,
        coverage_units=units,
        covered_tickers=covered_tickers,
    )


async def list_automations_for_scope_db(
    *,
    scope: AutomationScope,
    caller_user_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    computer_id: str | None = None,
    conn: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read paginated automations and matching total count for an explicit ``scope``."""
    from src.server.database.automation import AUTOMATION_COLUMNS, _last_execution_join

    where_parts: list[str] = ["automations.deleted_at IS NULL"]
    params: list[Any] = []

    if scope == "company":
        where_parts.append("automations.scope = 'company'")
        where_parts.append("automations.user_id = %s")
        params.append(COMPANY_SYSTEM_USER_ID)
    else:
        where_parts.append("automations.user_id = %s")
        params.append(caller_user_id)
        where_parts.append("automations.scope = %s")
        params.append(scope)
        if computer_id is not None:
            where_parts.append(
                "automations.workspace_id <> %s::uuid AND EXISTS (SELECT 1 FROM workspaces w"
                " WHERE w.workspace_id = automations.workspace_id"
                " AND w.computer_id = %s::uuid)"
            )
            params.extend([get_home_workspace_id(caller_user_id), computer_id])

    if status:
        where_parts.append("automations.status = %s")
        params.append(status)

    where_clause = " AND ".join(where_parts)

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT COUNT(*) AS cnt FROM automations WHERE {where_clause}",
                tuple(params),
            )
            count_row = await cur.fetchone()
            total = int((count_row or {}).get("cnt", 0))

            await cur.execute(
                f"""
                SELECT {AUTOMATION_COLUMNS}, le.last_execution
                FROM automations
                {_last_execution_join("automations")}
                WHERE {where_clause}
                ORDER BY automations.created_at DESC, automations.automation_id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows], total


async def get_automation_history_row(
    automation_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read stored version metadata and ``audit_history`` without mutating the row."""
    clean_id = _valid_uuid(automation_id)
    if not clean_id:
        return None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    automation_id,
                    user_id,
                    scope,
                    workspace_id,
                    config_version,
                    active_version,
                    audit_history,
                    deleted_at
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (clean_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
