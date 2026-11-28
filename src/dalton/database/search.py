"""Keyword global search across a user's owned workspaces and personal memos.

Scope is strictly limited to workspaces owned by the current user
(`workspaces.user_id = current_user AND workspaces.status != 'deleted'`) and
the user's personal memo namespace (`store.prefix = '<user_id>.memos'`).
Colleague-shared workspaces are intentionally excluded.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg.errors
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5s'"
CONTENT_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '15s'"
MEMO_INDEX_KEY = "memo.md"
MEMO_USER_PATH_PREFIX = ".agents/user/memo"


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards (``\\``, ``%``, ``_``) for ``ESCAPE '\\'``."""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _collapse_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _format_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _ts_sort_key(dt: datetime | None) -> float:
    if dt is None:
        return float("-inf")
    return dt.timestamp()


def build_snippet(
    text: str | None,
    query: str,
    *,
    context_chars: int = 60,
) -> tuple[str, list[list[int]]]:
    """Extract a ~2*context_chars snippet around the first match and return case-insensitive highlight spans."""
    collapsed = _collapse_whitespace(text)
    norm_query = _collapse_whitespace(query)
    if not collapsed:
        return "", []
    if not norm_query:
        if len(collapsed) > context_chars * 2:
            return collapsed[: context_chars * 2].rstrip() + "…", []
        return collapsed, []

    pattern = re.compile(re.escape(norm_query), re.IGNORECASE)
    first_match = pattern.search(collapsed)
    if first_match is None:
        if len(collapsed) > context_chars * 2:
            return collapsed[: context_chars * 2].rstrip() + "…", []
        return collapsed, []

    start = max(0, first_match.start() - context_chars)
    end = min(len(collapsed), first_match.end() + context_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(collapsed) else ""
    snippet = f"{prefix}{collapsed[start:end]}{suffix}"
    highlights = [[m.start(), m.end()] for m in pattern.finditer(snippet)]
    return snippet, highlights


def parse_optional_uuid(value: str | None) -> str | None:
    """Validate and canonicalize an optional UUID string before hitting Postgres."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return str(uuid.UUID(stripped))


async def _run_timed_query(
    db_conn: Any,
    sql: str,
    params: tuple[Any, ...],
    *,
    timeout_sql: str = STATEMENT_TIMEOUT_SQL,
) -> list[dict[str, Any]]:
    async with db_conn.transaction():
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(timeout_sql)
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or []) if isinstance(r, dict)]


_SQL_SEARCH_MEMOS = """
    SELECT
        s.prefix AS prefix,
        s.key AS memo_key,
        s.value AS value,
        s.updated_at AS updated_at,
        s.created_at AS created_at
    FROM store s
    WHERE s.prefix = %s
      AND s.key != %s
      AND (
          COALESCE(s.value->>'content', '') ILIKE %s ESCAPE '\\'
          OR COALESCE(s.value->>'original_filename', '') ILIKE %s ESCAPE '\\'
          OR COALESCE(s.value->>'description', '') ILIKE %s ESCAPE '\\'
      )
    ORDER BY COALESCE(s.updated_at, s.created_at) DESC NULLS LAST
    LIMIT %s
"""

def _build_workspace_files_sql(*, has_workspace_id: bool, include_content: bool) -> str:
    """Build the single parameterized workspace_files query without touching content_text when unneeded."""
    content_select = (
        "f.content_text AS content_text,"
        if include_content
        else "NULL::text AS content_text,"
    )
    workspace_clause = "      AND f.workspace_id = %s::uuid\n" if has_workspace_id else ""
    content_clause = (
        "          OR (f.content_text IS NOT NULL AND f.content_text ILIKE %s ESCAPE '\\')\n"
        if include_content
        else ""
    )
    return f"""
    SELECT
        f.workspace_file_id::text AS file_id,
        f.workspace_id::text AS workspace_id,
        w.name AS workspace_name,
        f.file_path AS file_path,
        f.file_name AS file_name,
        {content_select}
        f.sandbox_modified_at AS sandbox_modified_at,
        f.updated_at AS updated_at
    FROM workspace_files f
    JOIN workspaces w
      ON w.workspace_id = f.workspace_id
    WHERE w.user_id = %s
      AND w.status != 'deleted'
      AND f.kind = 'file'
{workspace_clause}      AND (
          f.file_name ILIKE %s ESCAPE '\\'
          OR f.file_path ILIKE %s ESCAPE '\\'
{content_clause}      )
    ORDER BY COALESCE(f.sandbox_modified_at, f.updated_at) DESC NULLS LAST
    LIMIT %s
"""


def _row_owned_and_active(row: dict[str, Any], user_id: str) -> bool:
    """Defensive ownership/status check when test doubles return unfiltered rows."""
    if "user_id" in row and row["user_id"] != user_id:
        return False
    status = row.get("workspace_status") or row.get("status")
    if status == "deleted":
        return False
    return True


async def _search_workspace_files_rows(
    db_conn: Any,
    user_id: str,
    like_pattern: str,
    clamped_limit: int,
    *,
    norm_workspace_id: str | None,
    include_content: bool,
) -> list[dict[str, Any]]:
    sql = _build_workspace_files_sql(
        has_workspace_id=norm_workspace_id is not None,
        include_content=include_content,
    )
    params_list: list[Any] = [user_id]
    if norm_workspace_id is not None:
        params_list.append(norm_workspace_id)
    params_list.extend([like_pattern, like_pattern])
    if include_content:
        params_list.append(like_pattern)
    params_list.append(clamped_limit)
    timeout_sql = CONTENT_STATEMENT_TIMEOUT_SQL if include_content else STATEMENT_TIMEOUT_SQL
    return await _run_timed_query(
        db_conn,
        sql,
        tuple(params_list),
        timeout_sql=timeout_sql,
    )


async def search_documents(
    user_id: str,
    query: str,
    limit: int = 20,
    *,
    workspace_id: str | None = None,
    include_content: bool = True,
    content_timed_out_out: list[bool] | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Search personal memos (``<user_id>.memos``) and owned ``workspace_files``."""
    norm_q = (query or "").strip()
    if len(norm_q) < 2:
        return []
    clamped_limit = max(1, min(int(limit), 50))
    invalid_workspace_id = False
    norm_workspace_id: str | None = None
    try:
        norm_workspace_id = parse_optional_uuid(workspace_id)
    except ValueError:
        invalid_workspace_id = True

    like_pattern = f"%{escape_like(norm_q)}%"
    memo_prefix = f"{user_id}.memos"

    async with get_db_connection(conn) as db_conn:
        memo_rows = await _run_timed_query(
            db_conn,
            _SQL_SEARCH_MEMOS,
            (memo_prefix, MEMO_INDEX_KEY, like_pattern, like_pattern, like_pattern, clamped_limit),
        )
        if invalid_workspace_id:
            file_rows: list[dict[str, Any]] = []
        elif include_content:
            try:
                file_rows = await _search_workspace_files_rows(
                    db_conn,
                    user_id,
                    like_pattern,
                    clamped_limit,
                    norm_workspace_id=norm_workspace_id,
                    include_content=True,
                )
            except psycopg.errors.QueryCanceled:
                logger.warning(
                    "Workspace file content search timed out (15s); falling back to filename/path matches",
                )
                if content_timed_out_out is not None:
                    content_timed_out_out.append(True)
                file_rows = await _search_workspace_files_rows(
                    db_conn,
                    user_id,
                    like_pattern,
                    clamped_limit,
                    norm_workspace_id=norm_workspace_id,
                    include_content=False,
                )
        else:
            file_rows = await _search_workspace_files_rows(
                db_conn,
                user_id,
                like_pattern,
                clamped_limit,
                norm_workspace_id=norm_workspace_id,
                include_content=False,
            )

    results: list[tuple[float, dict[str, Any]]] = []

    for row in memo_rows:
        row_prefix = row.get("prefix")
        if row_prefix is not None and row_prefix != memo_prefix:
            continue
        memo_key = str(row.get("memo_key") or row.get("key") or "")
        if not memo_key or memo_key == MEMO_INDEX_KEY:
            continue
        raw_val = row.get("value")
        if isinstance(raw_val, str):
            try:
                val = json.loads(raw_val)
            except ValueError:
                val = {}
        elif isinstance(raw_val, dict):
            val = raw_val
        else:
            val = {}

        content_str = str(val.get("content") or "")
        filename_str = str(val.get("original_filename") or "")
        desc_str = str(val.get("description") or "")

        chosen_snippet = ""
        chosen_highlights: list[list[int]] = []
        for candidate in (content_str, desc_str, filename_str):
            if not candidate:
                continue
            snip, hl = build_snippet(candidate, norm_q)
            if hl:
                chosen_snippet = snip
                chosen_highlights = hl
                break

        if not chosen_highlights:
            continue

        hit_dt = (
            _parse_ts(val.get("modified_at"))
            or _parse_ts(val.get("created_at"))
            or _parse_ts(row.get("updated_at"))
            or _parse_ts(row.get("created_at"))
        )
        title = filename_str or desc_str or memo_key
        source_ws_id = val.get("source_workspace_id")
        results.append(
            (
                _ts_sort_key(hit_dt),
                {
                    "kind": "memo",
                    "memo_key": memo_key,
                    "workspace_id": str(source_ws_id) if source_ws_id else None,
                    "path": f"{MEMO_USER_PATH_PREFIX}/{memo_key}",
                    "title": title,
                    "workspace_name": "",
                    "timestamp": _format_ts(hit_dt),
                    "snippet": chosen_snippet,
                    "highlights": chosen_highlights,
                },
            )
        )

    for row in file_rows:
        if not _row_owned_and_active(row, user_id):
            continue
        if "kind" in row and row["kind"] != "file":
            continue
        row_workspace_id = str(row.get("workspace_id") or "")
        if norm_workspace_id is not None and row_workspace_id != norm_workspace_id:
            continue
        workspace_name = str(row.get("workspace_name") or "")
        file_path = str(row.get("file_path") or "")
        file_name = str(row.get("file_name") or posixpath.basename(file_path))
        content_text = row.get("content_text")

        candidates: list[str] = []
        if include_content and isinstance(content_text, str) and content_text:
            candidates.append(content_text)
        if file_path:
            candidates.append(file_path)
        if file_name:
            candidates.append(file_name)

        chosen_snippet = ""
        chosen_highlights: list[list[int]] = []
        for candidate in candidates:
            snip, hl = build_snippet(candidate, norm_q)
            if hl:
                chosen_snippet = snip
                chosen_highlights = hl
                break

        if not chosen_highlights:
            continue

        hit_dt = _parse_ts(row.get("sandbox_modified_at")) or _parse_ts(row.get("updated_at"))
        results.append(
            (
                _ts_sort_key(hit_dt),
                {
                    "kind": "workspace_file",
                    "memo_key": None,
                    "workspace_id": row_workspace_id,
                    "path": file_path,
                    "title": file_name or file_path,
                    "workspace_name": workspace_name,
                    "timestamp": _format_ts(hit_dt),
                    "snippet": chosen_snippet,
                    "highlights": chosen_highlights,
                },
            )
        )

    results.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in results[:clamped_limit]]


async def global_search(
    user_id: str,
    query: str,
    limit: int = 20,
    *,
    workspace_id: str | None = None,
    include_content: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Run keyword document search across personal memos and owned workspace files."""
    norm_q = (query or "").strip()
    if len(norm_q) < 2:
        raise ValueError("Search query must be at least 2 characters after trimming whitespace.")
    clamped_limit = max(1, min(int(limit), 50))
    content_timed_out_flags: list[bool] = []
    documents = await search_documents(
        user_id,
        norm_q,
        limit=clamped_limit,
        workspace_id=workspace_id,
        include_content=include_content,
        content_timed_out_out=content_timed_out_flags,
        conn=conn,
    )
    payload: dict[str, Any] = {
        "documents": documents,
    }
    if content_timed_out_flags:
        payload["content_timed_out"] = True
    return payload
