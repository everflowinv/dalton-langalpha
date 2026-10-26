"""Keyword global search across a user's owned workspaces and personal memos.

Scope is strictly limited to workspaces owned by the current user
(`workspaces.user_id = current_user AND workspaces.status != 'deleted'`) and
the user's personal memo namespace (`store.prefix = '<user_id>.memos'`).
Colleague-shared workspaces are intentionally excluded.
"""

from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5s'"
MEMO_INDEX_KEY = "memo.md"
MEMO_USER_PATH_PREFIX = ".agents/user/memo"
TRANSCRIPT_BATCH_SIZE = 200
TRANSCRIPT_MAX_SCANNED_FILES = 2000
_TURN_FILE_RE = re.compile(r"^turn-\d{4}\.jsonl$")


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


def extract_transcript_turn_hit(
    raw_jsonl: bytes | bytearray | memoryview | str | None,
    query: str,
    stored_at: Any = None,
) -> tuple[str, datetime | None] | None:
    """Parse a ``turn-NNNN.jsonl`` payload and match only ``user`` / ``assistant`` ``text`` fields.

    Never searches ``tool_call`` or ``tool_result`` records, and never returns raw JSON lines.
    Timestamp prefers the ``at`` field of the first ``user`` record in the turn, falling back to ``stored_at``.
    """
    if raw_jsonl is None:
        return None
    if isinstance(raw_jsonl, (bytes, bytearray, memoryview)):
        decoded = bytes(raw_jsonl).decode("utf-8", errors="replace")
    elif isinstance(raw_jsonl, str):
        decoded = raw_jsonl
    else:
        return None

    norm_query = _collapse_whitespace(query)
    if not norm_query:
        return None
    pattern = re.compile(re.escape(norm_query), re.IGNORECASE)

    first_user_at: datetime | None = None
    matched_text: str | None = None

    for raw_line in decoded.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue

        rec_type = record.get("type")
        if rec_type == "user" and first_user_at is None:
            first_user_at = _parse_ts(record.get("at"))

        if rec_type not in ("user", "assistant"):
            continue

        text_val = record.get("text")
        if not isinstance(text_val, str):
            continue
        collapsed_text = _collapse_whitespace(text_val)
        if collapsed_text and pattern.search(collapsed_text):
            matched_text = text_val

    if matched_text is None:
        return None

    turn_ts = first_user_at if first_user_at is not None else _parse_ts(stored_at)
    return matched_text, turn_ts


async def _run_timed_query(
    db_conn: Any,
    sql: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    async with db_conn.transaction():
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(STATEMENT_TIMEOUT_SQL)
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in (rows or []) if isinstance(r, dict)]


_SQL_SEARCH_THREAD_TITLES = """
    SELECT
        t.conversation_thread_id::text AS thread_id,
        t.workspace_id::text AS workspace_id,
        COALESCE(t.title, '') AS title,
        w.name AS workspace_name,
        COALESCE(t.updated_at, t.created_at) AS hit_at
    FROM conversation_threads t
    JOIN workspaces w
      ON w.workspace_id = t.workspace_id
    WHERE w.user_id = %s
      AND w.status != 'deleted'
      AND t.archived_at IS NULL
      AND t.title ILIKE %s ESCAPE '\\'
    ORDER BY COALESCE(t.updated_at, t.created_at) DESC NULLS LAST
    LIMIT %s
"""

_SQL_SEARCH_CONVERSATION_QUERIES = """
    SELECT * FROM (
        SELECT DISTINCT ON (t.conversation_thread_id)
            t.conversation_thread_id::text AS thread_id,
            t.workspace_id::text AS workspace_id,
            COALESCE(t.title, '') AS title,
            w.name AS workspace_name,
            q.content AS content,
            q.created_at AS created_at,
            q.turn_index AS turn_index
        FROM conversation_queries q
        JOIN conversation_threads t
          ON t.conversation_thread_id = q.conversation_thread_id
        JOIN workspaces w
          ON w.workspace_id = t.workspace_id
        WHERE w.user_id = %s
          AND w.status != 'deleted'
          AND t.archived_at IS NULL
          AND q.content ILIKE %s ESCAPE '\\'
        ORDER BY t.conversation_thread_id, q.created_at DESC NULLS LAST, q.turn_index DESC
    ) sub
    ORDER BY created_at DESC NULLS LAST, turn_index DESC
    LIMIT %s
"""

_SQL_SEARCH_TRANSCRIPT_FILES = """
    SELECT
        t.conversation_thread_id::text AS thread_id,
        t.workspace_id::text AS workspace_id,
        COALESCE(t.title, '') AS title,
        w.name AS workspace_name,
        f.path AS path,
        f.content AS content,
        COALESCE(tt.stored_at, t.updated_at, t.created_at) AS stored_at
    FROM thread_transcript_files f
    JOIN conversation_threads t
      ON t.conversation_thread_id = f.conversation_thread_id
    JOIN workspaces w
      ON w.workspace_id = t.workspace_id
    LEFT JOIN thread_transcripts tt
      ON tt.conversation_thread_id = f.conversation_thread_id
     AND tt.prefix = f.prefix
    WHERE w.user_id = %s
      AND w.status != 'deleted'
      AND t.archived_at IS NULL
      AND f.content IS NOT NULL
      AND f.path ~ '^turn-[0-9]{4}\\.jsonl$'
      AND (
          convert_from(f.content, 'UTF8') ILIKE %s ESCAPE '\\'
          OR convert_from(f.content, 'UTF8') ILIKE %s ESCAPE '\\'
      )
    ORDER BY COALESCE(tt.stored_at, t.updated_at, t.created_at) DESC NULLS LAST,
             t.conversation_thread_id DESC,
             f.path DESC
    LIMIT %s OFFSET %s
"""

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

_SQL_SEARCH_WORKSPACE_FILES = """
    SELECT
        f.workspace_file_id::text AS file_id,
        f.workspace_id::text AS workspace_id,
        w.name AS workspace_name,
        f.file_path AS file_path,
        f.file_name AS file_name,
        f.content_text AS content_text,
        f.sandbox_modified_at AS sandbox_modified_at,
        f.updated_at AS updated_at
    FROM workspace_files f
    JOIN workspaces w
      ON w.workspace_id = f.workspace_id
    WHERE w.user_id = %s
      AND w.status != 'deleted'
      AND f.kind = 'file'
      AND (
          f.file_name ILIKE %s ESCAPE '\\'
          OR f.file_path ILIKE %s ESCAPE '\\'
          OR (f.content_text IS NOT NULL AND f.content_text ILIKE %s ESCAPE '\\')
      )
    ORDER BY COALESCE(f.sandbox_modified_at, f.updated_at) DESC NULLS LAST
    LIMIT %s
"""


def _row_owned_and_active(row: dict[str, Any], user_id: str, *, check_archived: bool = False) -> bool:
    """Defensive ownership/status check when test doubles return unfiltered rows."""
    if "user_id" in row and row["user_id"] != user_id:
        return False
    status = row.get("workspace_status") or row.get("status")
    if status == "deleted":
        return False
    if check_archived and row.get("archived_at") is not None:
        return False
    return True


async def search_conversations(
    user_id: str,
    query: str,
    limit: int = 20,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Search conversation titles, user queries, and transcript user/assistant text for owned workspaces."""
    norm_q = (query or "").strip()
    if len(norm_q) < 2:
        return []
    clamped_limit = max(1, min(int(limit), 50))
    like_pattern = f"%{escape_like(norm_q)}%"
    json_escaped_q = json.dumps(norm_q, ensure_ascii=False)[1:-1]
    json_like_pattern = f"%{escape_like(json_escaped_q)}%"

    # Deduplicate by thread_id, keeping the single most recent hit per conversation.
    # Candidate tuple: (ts_sort_key, source_priority, secondary_order, hit_dt, raw_text, meta_dict)
    best_by_thread: dict[str, tuple[float, int, str, datetime | None, str, dict[str, Any]]] = {}

    def _consider_hit(
        thread_id: str,
        workspace_id: str,
        title: str,
        workspace_name: str,
        raw_text: str,
        hit_dt: datetime | None,
        *,
        source_priority: int,
        secondary_key: str = "",
    ) -> None:
        if not thread_id:
            return
        snippet, highlights = build_snippet(raw_text, norm_q)
        if not highlights:
            return
        sort_ts = _ts_sort_key(hit_dt)
        candidate_key = (sort_ts, source_priority, secondary_key)
        existing = best_by_thread.get(thread_id)
        if existing is not None:
            existing_key = (existing[0], existing[1], existing[2])
            if candidate_key <= existing_key:
                return
        best_by_thread[thread_id] = (
            sort_ts,
            source_priority,
            secondary_key,
            hit_dt,
            raw_text,
            {
                "kind": "conversation",
                "thread_id": thread_id,
                "workspace_id": workspace_id,
                "title": title,
                "workspace_name": workspace_name,
                "timestamp": _format_ts(hit_dt),
                "snippet": snippet,
                "highlights": highlights,
            },
        )

    async with get_db_connection(conn) as db_conn:
        title_rows = await _run_timed_query(
            db_conn,
            _SQL_SEARCH_THREAD_TITLES,
            (user_id, like_pattern, clamped_limit),
        )
        query_rows = await _run_timed_query(
            db_conn,
            _SQL_SEARCH_CONVERSATION_QUERIES,
            (user_id, like_pattern, clamped_limit),
        )

        for row in title_rows:
            if not _row_owned_and_active(row, user_id, check_archived=True):
                continue
            thread_id = str(row.get("thread_id") or row.get("conversation_thread_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            title = str(row.get("title") or "")
            workspace_name = str(row.get("workspace_name") or "")
            hit_dt = _parse_ts(row.get("hit_at") or row.get("updated_at") or row.get("created_at"))
            _consider_hit(
                thread_id,
                workspace_id,
                title,
                workspace_name,
                title,
                hit_dt,
                source_priority=1,
            )

        for row in query_rows:
            if not _row_owned_and_active(row, user_id, check_archived=True):
                continue
            thread_id = str(row.get("thread_id") or row.get("conversation_thread_id") or "")
            workspace_id = str(row.get("workspace_id") or "")
            title = str(row.get("title") or "")
            workspace_name = str(row.get("workspace_name") or "")
            content = str(row.get("content") or "")
            hit_dt = _parse_ts(row.get("created_at") or row.get("hit_at"))
            turn_idx = int(row.get("turn_index") or 0)
            _consider_hit(
                thread_id,
                workspace_id,
                title,
                workspace_name,
                content,
                hit_dt,
                source_priority=2,
                secondary_key=f"{turn_idx:08d}",
            )

        scanned_files = 0
        offset = 0
        transcript_threads_matched: set[str] = set()
        while (
            scanned_files < TRANSCRIPT_MAX_SCANNED_FILES
            and len(transcript_threads_matched) < clamped_limit
        ):
            batch_limit = min(
                TRANSCRIPT_BATCH_SIZE,
                TRANSCRIPT_MAX_SCANNED_FILES - scanned_files,
            )
            transcript_rows = await _run_timed_query(
                db_conn,
                _SQL_SEARCH_TRANSCRIPT_FILES,
                (user_id, like_pattern, json_like_pattern, batch_limit, offset),
            )
            if not transcript_rows:
                break
            for row in transcript_rows:
                if not _row_owned_and_active(row, user_id, check_archived=True):
                    continue
                path = str(row.get("path") or "")
                if path and not _TURN_FILE_RE.match(posixpath.basename(path)):
                    continue
                raw_content = row.get("content")
                if raw_content is None:
                    continue
                extracted = extract_transcript_turn_hit(
                    raw_content,
                    norm_q,
                    stored_at=row.get("stored_at"),
                )
                if extracted is None:
                    continue
                matched_text, hit_dt = extracted
                thread_id = str(row.get("thread_id") or row.get("conversation_thread_id") or "")
                workspace_id = str(row.get("workspace_id") or "")
                title = str(row.get("title") or "")
                workspace_name = str(row.get("workspace_name") or "")
                if thread_id:
                    transcript_threads_matched.add(thread_id)
                _consider_hit(
                    thread_id,
                    workspace_id,
                    title,
                    workspace_name,
                    matched_text,
                    hit_dt,
                    source_priority=3,
                    secondary_key=path,
                )
            scanned_files += len(transcript_rows)
            offset += len(transcript_rows)
            if len(transcript_rows) < batch_limit:
                break

    sorted_entries = sorted(
        best_by_thread.values(),
        key=lambda item: (item[0], item[1], item[2]),
        reverse=True,
    )
    return [entry[5] for entry in sorted_entries[:clamped_limit]]


async def search_documents(
    user_id: str,
    query: str,
    limit: int = 20,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Search personal memos (``<user_id>.memos``) and owned ``workspace_files``."""
    norm_q = (query or "").strip()
    if len(norm_q) < 2:
        return []
    clamped_limit = max(1, min(int(limit), 50))
    like_pattern = f"%{escape_like(norm_q)}%"
    memo_prefix = f"{user_id}.memos"

    async with get_db_connection(conn) as db_conn:
        memo_rows = await _run_timed_query(
            db_conn,
            _SQL_SEARCH_MEMOS,
            (memo_prefix, MEMO_INDEX_KEY, like_pattern, like_pattern, like_pattern, clamped_limit),
        )
        file_rows = await _run_timed_query(
            db_conn,
            _SQL_SEARCH_WORKSPACE_FILES,
            (user_id, like_pattern, like_pattern, like_pattern, clamped_limit),
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
        if not _row_owned_and_active(row, user_id, check_archived=False):
            continue
        if "kind" in row and row["kind"] != "file":
            continue
        workspace_id = str(row.get("workspace_id") or "")
        workspace_name = str(row.get("workspace_name") or "")
        file_path = str(row.get("file_path") or "")
        file_name = str(row.get("file_name") or posixpath.basename(file_path))
        content_text = row.get("content_text")

        candidates: list[str] = []
        if isinstance(content_text, str) and content_text:
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
                    "workspace_id": workspace_id,
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
    conn: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run keyword global search across owned conversations and documents."""
    norm_q = (query or "").strip()
    if len(norm_q) < 2:
        raise ValueError("Search query must be at least 2 characters after trimming whitespace.")
    clamped_limit = max(1, min(int(limit), 50))
    conversations = await search_conversations(user_id, norm_q, limit=clamped_limit, conn=conn)
    documents = await search_documents(user_id, norm_q, limit=clamped_limit, conn=conn)
    return {
        "conversations": conversations,
        "documents": documents,
    }
