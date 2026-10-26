"""Unit tests for global keyword search (`GET /api/v1/dalton/search` & `src/dalton/database/search.py`)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import AsyncConnection, AsyncCursor
import pytest

from src.dalton.app import dalton_router
import src.dalton.database.search as search_db
from src.dalton.database.search import (
    STATEMENT_TIMEOUT_SQL,
    build_snippet,
    escape_like,
    extract_transcript_turn_hit,
    global_search,
)
from src.server.utils.api import get_current_user_id


def _make_fake_db(
    query_responder: Any,
) -> tuple[Any, list[tuple[str, tuple[Any, ...] | None]]]:
    """Build autospecced AsyncConnection + AsyncCursor recording all executed SQL statements."""
    executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def _make_cursor(*args: Any, **kwargs: Any) -> Any:
        cur = create_autospec(AsyncCursor, instance=True)
        last_rows: list[dict[str, Any]] = []

        async def _execute(sql: str, params: tuple[Any, ...] | None = None, **kw: Any) -> None:
            nonlocal last_rows
            executed.append((sql, params))
            if sql.strip() == STATEMENT_TIMEOUT_SQL:
                last_rows = []
            else:
                last_rows = list(query_responder(sql, params) or [])

        async def _fetchall() -> list[dict[str, Any]]:
            return last_rows

        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.__aenter__.return_value = cur
        cur.__aexit__.return_value = False
        return cur

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _make_cursor

    @asynccontextmanager
    async def _tx(*args: Any, **kwargs: Any):
        yield

    conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _get_conn(existing_conn: Any = None):
        yield existing_conn or conn

    fake_get_conn = create_autospec(search_db.get_db_connection, side_effect=_get_conn)
    return fake_get_conn, executed


@pytest.mark.asyncio
async def test_only_returns_owned_active_workspaces_and_unarchived_conversations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Excludes other users' workspaces, colleague-shared workspaces, deleted workspaces, and archived threads."""

    def _responder(sql: str, params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        assert params is not None
        assert "w.user_id = %s" in sql or "s.prefix = %s" in sql
        if "FROM conversation_threads t" in sql and "conversation_queries" not in sql and "thread_transcript_files" not in sql:
            assert "w.status != 'deleted'" in sql
            assert "t.archived_at IS NULL" in sql
            return [
                {
                    "thread_id": "thread-owned",
                    "workspace_id": "ws-owned",
                    "user_id": "user-alice",
                    "status": "running",
                    "archived_at": None,
                    "title": "NVDA Earnings Call",
                    "workspace_name": "NVDA",
                    "hit_at": datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc),
                },
                {
                    "thread_id": "thread-other",
                    "workspace_id": "ws-other",
                    "user_id": "user-bob",
                    "status": "running",
                    "archived_at": None,
                    "title": "NVDA Other User",
                    "workspace_name": "Bob WS",
                    "hit_at": datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
                },
                {
                    "thread_id": "thread-shared",
                    "workspace_id": "ws-shared",
                    "user_id": "user-colleague",
                    "status": "running",
                    "archived_at": None,
                    "title": "NVDA Shared Team Workspace",
                    "workspace_name": "Shared WS",
                    "hit_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                },
                {
                    "thread_id": "thread-deleted-ws",
                    "workspace_id": "ws-deleted",
                    "user_id": "user-alice",
                    "status": "deleted",
                    "archived_at": None,
                    "title": "NVDA Deleted Workspace",
                    "workspace_name": "Deleted WS",
                    "hit_at": datetime(2026, 10, 26, 11, 30, tzinfo=timezone.utc),
                },
                {
                    "thread_id": "thread-archived",
                    "workspace_id": "ws-owned",
                    "user_id": "user-alice",
                    "status": "running",
                    "archived_at": datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                    "title": "NVDA Archived Thread",
                    "workspace_name": "NVDA",
                    "hit_at": datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),
                },
            ]
        if "FROM workspace_files f" in sql:
            assert "w.user_id = %s" in sql
            assert "w.status != 'deleted'" in sql
            return [
                {
                    "file_id": "file-owned",
                    "workspace_id": "ws-owned",
                    "user_id": "user-alice",
                    "status": "running",
                    "workspace_name": "NVDA",
                    "file_path": "reports/nvda_model.md",
                    "file_name": "nvda_model.md",
                    "content_text": "NVDA datacenter revenue forecast",
                    "sandbox_modified_at": datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc),
                },
                {
                    "file_id": "file-shared",
                    "workspace_id": "ws-shared",
                    "user_id": "user-colleague",
                    "status": "running",
                    "workspace_name": "Shared WS",
                    "file_path": "reports/nvda_shared.md",
                    "file_name": "nvda_shared.md",
                    "content_text": "NVDA shared note",
                    "sandbox_modified_at": datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
                },
                {
                    "file_id": "file-deleted-ws",
                    "workspace_id": "ws-deleted",
                    "user_id": "user-alice",
                    "status": "deleted",
                    "workspace_name": "Deleted WS",
                    "file_path": "reports/nvda_deleted.md",
                    "file_name": "nvda_deleted.md",
                    "content_text": "NVDA deleted workspace file",
                    "sandbox_modified_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                },
            ]
        return []

    fake_get_conn, _ = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search("user-alice", "NVDA", limit=20)
    assert [c["thread_id"] for c in res["conversations"]] == ["thread-owned"]
    assert [d["path"] for d in res["documents"]] == ["reports/nvda_model.md"]


@pytest.mark.asyncio
async def test_memo_only_queries_current_user_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memos are queried strictly under `<user_id>.memos` and exclude other users' memos and `memo.md`."""
    captured_memo_params: list[tuple[Any, ...]] = []

    def _responder(sql: str, params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM store s" in sql:
            assert params is not None
            captured_memo_params.append(params)
            return [
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "q3-notes.md",
                    "value": {
                        "original_filename": "Q3 Notes.md",
                        "description": "Quarterly review",
                        "content": "TSMC wafer allocation details for 2026.",
                        "modified_at": "2026-10-26T07:30:00Z",
                        "created_at": "2026-10-20T07:30:00Z",
                    },
                },
                {
                    "prefix": "user-bob.memos",
                    "memo_key": "bob-secret.md",
                    "value": {
                        "original_filename": "Bob Secret.md",
                        "content": "TSMC wafer confidential.",
                        "modified_at": "2026-10-26T08:30:00Z",
                    },
                },
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "memo.md",
                    "value": {
                        "content": "TSMC index catalog",
                    },
                },
            ]
        return []

    fake_get_conn, _ = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search("user-alice", "TSMC", limit=20)
    assert len(captured_memo_params) == 1
    assert captured_memo_params[0][0] == "user-alice.memos"
    assert captured_memo_params[0][1] == "memo.md"
    assert len(res["documents"]) == 1
    doc = res["documents"][0]
    assert doc["kind"] == "memo"
    assert doc["memo_key"] == "q3-notes.md"
    assert doc["path"] == ".agents/user/memo/q3-notes.md"
    assert doc["title"] == "Q3 Notes.md"
    assert doc["timestamp"] == "2026-10-26T07:30:00+00:00"


@pytest.mark.asyncio
async def test_wildcard_escaping_statement_timeout_and_short_query_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escapes `%`, `_`, and `\\` in ILIKE patterns, sets 5s statement_timeout per query, and returns 422 on <2 char queries."""
    assert escape_like(r"50%_off\deal") == r"50\%\_off\\deal"

    fake_get_conn, executed = _make_fake_db(lambda sql, params: [])
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    await global_search("user-alice", r"50%_off\deal", limit=15)

    timeout_calls = [sql for sql, _ in executed if sql.strip() == STATEMENT_TIMEOUT_SQL]
    select_calls = [(sql, params) for sql, params in executed if "SELECT" in sql]
    assert len(select_calls) == 5
    assert len(timeout_calls) == len(select_calls)
    for sql, params in select_calls:
        assert "LIMIT %s" in sql
        assert params is not None
        assert r"%50\%\_off\\deal%" in params

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "user-alice"
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    for short_q in ("", "   ", "a", " 中 "):
        resp = client.get("/api/v1/dalton/search", params={"q": short_q})
        assert resp.status_code == 422, f"Expected 422 for q={short_q!r}, got {resp.status_code}"

    resp_zh = client.get("/api/v1/dalton/search", params={"q": "营收"})
    assert resp_zh.status_code == 200
    assert resp_zh.json() == {"conversations": [], "documents": []}


def test_transcript_jsonl_only_uses_user_and_assistant_text_and_highlights_accurately() -> None:
    """Transcript parser ignores `tool_call`/`tool_result` and raw JSON keys, and computes accurate snippet highlights."""
    lines = [
        json.dumps(
            {
                "type": "user",
                "text": "Can you check the gross margin trajectory?",
                "at": "2026-10-26T04:15:00Z",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "fetch_secret_table",
                "args": {"metric": "secret_internal_token"},
            }
        ),
        json.dumps(
            {
                "type": "tool_result",
                "tool_call_id": "call_1",
                "output": "secret_internal_token = 9999",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "text": (
                    "Prefix padding text that is well over sixty characters long so the left side is truncated cleanly. "
                    "The Gross Margin expanded by 340 bps year over year driven by Hopper mix. "
                    "Suffix padding text that is also well over sixty characters long so the right side is truncated cleanly."
                ),
            }
        ),
    ]
    raw_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    fallback_stored_at = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)

    # Tool names, args, outputs, and JSON structural keys must not match
    assert extract_transcript_turn_hit(raw_bytes, "fetch_secret_table", fallback_stored_at) is None
    assert extract_transcript_turn_hit(raw_bytes, "secret_internal_token", fallback_stored_at) is None
    assert extract_transcript_turn_hit(raw_bytes, "tool_call", fallback_stored_at) is None

    # Assistant text match uses first user line's `at` timestamp
    hit = extract_transcript_turn_hit(raw_bytes, "gross margin", fallback_stored_at)
    assert hit is not None
    matched_text, hit_ts = hit
    assert hit_ts == datetime(2026, 10, 26, 4, 15, tzinfo=timezone.utc)
    assert "tool_call" not in matched_text
    assert not matched_text.lstrip().startswith("{")

    snippet, highlights = build_snippet(matched_text, "gross margin")
    assert snippet.startswith("…")
    assert snippet.endswith("…")
    assert len(highlights) >= 1
    for start, end in highlights:
        assert snippet[start:end].lower() == "gross margin"

    # Fallback to stored_at when user line has no `at`
    assistant_only = json.dumps({"type": "assistant", "text": "Standalone assistant summary."}).encode("utf-8")
    hit2 = extract_transcript_turn_hit(assistant_only, "standalone", fallback_stored_at)
    assert hit2 is not None
    assert hit2[1] == fallback_stored_at


@pytest.mark.asyncio
async def test_deduplicates_per_thread_and_sorts_both_sections_by_time_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each conversation appears at most once (using its latest hit), and both sections sort by timestamp DESC."""
    turn_jsonl_t1 = "\n".join(
        [
            json.dumps(
                {
                    "type": "user",
                    "text": "Update on CapEx guidance?",
                    "at": "2026-10-26T10:30:00Z",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "text": "Management raised CapEx guidance to $40B.",
                }
            ),
        ]
    ).encode("utf-8")

    def _responder(sql: str, params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM conversation_threads t" in sql and "conversation_queries" not in sql and "thread_transcript_files" not in sql:
            return [
                {
                    "thread_id": "thread-1",
                    "workspace_id": "ws-1",
                    "title": "CapEx Deep Dive",
                    "workspace_name": "MSFT",
                    "hit_at": datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc),
                },
                {
                    "thread_id": "thread-2",
                    "workspace_id": "ws-2",
                    "title": "CapEx Peer Comparison",
                    "workspace_name": "GOOG",
                    "hit_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                },
            ]
        if "FROM conversation_queries q" in sql:
            return [
                {
                    "thread_id": "thread-1",
                    "workspace_id": "ws-1",
                    "title": "CapEx Deep Dive",
                    "workspace_name": "MSFT",
                    "content": "Initial question about CapEx in 2025",
                    "created_at": datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc),
                    "turn_index": 0,
                }
            ]
        if "FROM thread_transcript_files f" in sql:
            return [
                {
                    "thread_id": "thread-1",
                    "workspace_id": "ws-1",
                    "title": "CapEx Deep Dive",
                    "workspace_name": "MSFT",
                    "path": "turn-0002.jsonl",
                    "content": turn_jsonl_t1,
                    "stored_at": datetime(2026, 10, 26, 10, 31, tzinfo=timezone.utc),
                }
            ]
        if "FROM store s" in sql:
            return [
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "memo-older.md",
                    "value": {
                        "original_filename": "Older Memo.md",
                        "content": "CapEx memo from yesterday",
                        "created_at": "2026-10-25T12:00:00Z",
                    },
                },
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "memo-newest.md",
                    "value": {
                        "original_filename": "Newest Memo.md",
                        "content": "CapEx memo just updated",
                        "modified_at": "2026-10-26T11:45:00Z",
                        "created_at": "2026-10-24T12:00:00Z",
                    },
                },
            ]
        if "FROM workspace_files f" in sql:
            return [
                {
                    "file_id": "f-mid",
                    "workspace_id": "ws-1",
                    "workspace_name": "MSFT",
                    "file_path": "notes/capex_tracker.md",
                    "file_name": "capex_tracker.md",
                    "content_text": "Hyperscaler CapEx tracker table",
                    "sandbox_modified_at": datetime(2026, 10, 26, 9, 15, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc),
                }
            ]
        return []

    fake_get_conn, _ = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search("user-alice", "CapEx", limit=20)

    convs = res["conversations"]
    assert [c["thread_id"] for c in convs] == ["thread-2", "thread-1"]
    assert convs[0]["timestamp"] == "2026-10-26T11:00:00+00:00"
    # thread-1 deduplicated to its latest hit (10:30 transcript turn)
    assert convs[1]["timestamp"] == "2026-10-26T10:30:00+00:00"
    assert "$40B" in convs[1]["snippet"]

    docs = res["documents"]
    assert [d["title"] for d in docs] == [
        "Newest Memo.md",
        "capex_tracker.md",
        "Older Memo.md",
    ]


@pytest.mark.asyncio
async def test_transcript_search_json_escaped_queries_and_batched_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-filters transcript SQL with both raw and JSON-escaped LIKE patterns and scans across batches when tool-only files fill the first batch."""
    captured_transcript_params: list[tuple[Any, ...]] = []

    tool_only_bytes = (
        json.dumps(
            {
                "type": "tool_result",
                "output": r'Found ticker "NVDA" in C:\temp\report_100%.txt',
            }
        )
        + "\n"
    ).encode("utf-8")

    real_hit_bytes = (
        json.dumps(
            {
                "type": "assistant",
                "text": r'Saved "NVDA" summary under C:\temp\report_100%.txt for review.',
            }
        )
        + "\n"
    ).encode("utf-8")

    def _responder(sql: str, params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM thread_transcript_files f" in sql:
            assert params is not None
            captured_transcript_params.append(params)
            batch_limit = int(params[3])
            offset = int(params[4])
            if offset == 0:
                # First batch is completely filled with tool-only matches that get filtered out in Python
                return [
                    {
                        "thread_id": f"thread-tool-{i}",
                        "workspace_id": "ws-1",
                        "title": f"Tool Thread {i}",
                        "workspace_name": "NVDA",
                        "path": "turn-0001.jsonl",
                        "content": tool_only_bytes,
                        "stored_at": datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
                    }
                    for i in range(batch_limit)
                ]
            if offset == batch_limit:
                # Second batch contains the real assistant text hit
                return [
                    {
                        "thread_id": "thread-real-hit",
                        "workspace_id": "ws-1",
                        "title": "Real Hit Thread",
                        "workspace_name": "NVDA",
                        "path": "turn-0001.jsonl",
                        "content": real_hit_bytes,
                        "stored_at": datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc),
                    }
                ]
        return []

    fake_get_conn, _ = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    query_str = r'"NVDA" summary under C:\temp\report_100%'
    res = await global_search("user-alice", query_str, limit=5)

    # Should have fetched 2 batches because batch 1 had 0 valid user/assistant hits
    assert len(captured_transcript_params) == 2
    first_params = captured_transcript_params[0]
    expected_raw_like = f"%{escape_like(query_str)}%"
    expected_json_like = f"%{escape_like(json.dumps(query_str)[1:-1])}%"
    assert first_params[1] == expected_raw_like
    assert first_params[2] == expected_json_like

    assert [c["thread_id"] for c in res["conversations"]] == ["thread-real-hit"]
    snippet = res["conversations"][0]["snippet"]
    highlights = res["conversations"][0]["highlights"]
    assert len(highlights) == 1
    start, end = highlights[0]
    assert snippet[start:end] == query_str

