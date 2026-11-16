"""Unit tests for global keyword search (`GET /api/v1/dalton/search` & `src/dalton/database/search.py`)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg.errors
from psycopg import AsyncConnection, AsyncCursor
import pytest

from src.dalton.app import dalton_router
import src.dalton.database.search as search_db
from src.dalton.database.search import (
    CONTENT_STATEMENT_TIMEOUT_SQL,
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
            if sql.strip() in (STATEMENT_TIMEOUT_SQL, CONTENT_STATEMENT_TIMEOUT_SQL):
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

    timeout_calls = [
        sql for sql, _ in executed if sql.strip() in (STATEMENT_TIMEOUT_SQL, CONTENT_STATEMENT_TIMEOUT_SQL)
    ]
    select_calls = [(sql, params) for sql, params in executed if "SELECT" in sql]
    assert len(select_calls) == 5
    assert len(timeout_calls) == len(select_calls)
    assert any(sql.strip() == CONTENT_STATEMENT_TIMEOUT_SQL for sql in timeout_calls)
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


@pytest.mark.asyncio
async def test_global_search_skip_conversations_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting include_conversations=False skips conversation queries and returns documents only."""
    fake_search_conv = create_autospec(search_db.search_conversations)
    monkeypatch.setattr(search_db, "search_conversations", fake_search_conv)

    def _responder(sql: str, _params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM store s" in sql:
            return [
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "nvda.md",
                    "value": {
                        "original_filename": "NVDA memo",
                        "content": "NVDA AI chip thesis",
                        "modified_at": "2026-10-26T12:00:00Z",
                    },
                }
            ]
        return []

    fake_get_conn, executed = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search("user-alice", "NVDA", limit=10, include_conversations=False)

    fake_search_conv.assert_not_awaited()
    assert res["conversations"] == []
    assert [d["kind"] for d in res["documents"]] == ["memo"]
    assert not any("FROM threads" in sql for sql, _ in executed)


def test_router_forwards_include_conversations_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/dalton/search forwards include_conversations (default True) to global_search."""
    fake_search = create_autospec(
        search_db.global_search,
        return_value={"conversations": [], "documents": []},
    )
    monkeypatch.setattr(search_db, "global_search", fake_search)

    app = FastAPI()
    app.include_router(dalton_router)
    app.dependency_overrides[get_current_user_id] = lambda: "user-alice"
    monkeypatch.setattr("src.dalton.app.router._require_dalton_enabled", lambda: None)

    client = TestClient(app)

    r1 = client.get("/api/v1/dalton/search", params={"q": "memo"})
    assert r1.status_code == 200
    assert fake_search.await_args.kwargs["include_conversations"] is True
    assert fake_search.await_args.kwargs["include_content"] is True

    r2 = client.get(
        "/api/v1/dalton/search",
        params={"q": "memo", "limit": 15, "include_conversations": "false", "include_content": "false"},
    )
    assert r2.status_code == 200
    assert fake_search.await_args.kwargs["limit"] == 15
    assert fake_search.await_args.kwargs["include_conversations"] is False
    assert fake_search.await_args.kwargs["include_content"] is False
    assert fake_search.await_args.kwargs["workspace_id"] is None

    ws_uuid = "11111111-1111-4111-8111-111111111111"
    r3 = client.get(
        "/api/v1/dalton/search",
        params={"q": "memo", "workspace_id": ws_uuid, "include_conversations": "false"},
    )
    assert r3.status_code == 200
    assert fake_search.await_args.kwargs["workspace_id"] == ws_uuid


@pytest.mark.asyncio
async def test_global_search_filters_workspace_files_by_workspace_id_before_limit_and_keeps_memos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When workspace_id is provided, file SQL filters user_id + f.workspace_id = %s::uuid before LIMIT while memos remain personal."""
    ws_uuid = "11111111-1111-4111-8111-111111111111"
    ws_other_uuid = "22222222-2222-4222-8222-222222222222"
    captured_file_queries: list[tuple[str, tuple[Any, ...] | None]] = []

    def _responder(sql: str, params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM store s" in sql:
            return [
                {
                    "prefix": "user-alice.memos",
                    "memo_key": "personal.md",
                    "value": {
                        "original_filename": "Personal Memo",
                        "content": "NVDA wafer margin",
                        "modified_at": "2026-10-26T12:00:00Z",
                    },
                }
            ]
        if "FROM workspace_files f" in sql:
            captured_file_queries.append((sql, params))
            return [
                {
                    "file_id": "f-target",
                    "workspace_id": ws_uuid,
                    "user_id": "user-alice",
                    "status": "running",
                    "workspace_name": "Semis",
                    "file_path": "notes/target.md",
                    "file_name": "target.md",
                    "content_text": "NVDA target workspace file",
                    "sandbox_modified_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                },
                {
                    "file_id": "f-other-ws",
                    "workspace_id": ws_other_uuid,
                    "user_id": "user-alice",
                    "status": "running",
                    "workspace_name": "Other",
                    "file_path": "notes/other.md",
                    "file_name": "other.md",
                    "content_text": "NVDA other workspace file",
                    "sandbox_modified_at": datetime(2026, 10, 26, 11, 30, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 11, 30, tzinfo=timezone.utc),
                },
                {
                    "file_id": "f-foreign",
                    "workspace_id": ws_uuid,
                    "user_id": "user-bob",
                    "status": "running",
                    "workspace_name": "Spoofed",
                    "file_path": "notes/foreign.md",
                    "file_name": "foreign.md",
                    "content_text": "NVDA foreign user file",
                    "sandbox_modified_at": datetime(2026, 10, 26, 11, 45, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 11, 45, tzinfo=timezone.utc),
                },
            ]
        return []

    fake_get_conn, _ = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search(
        "user-alice",
        "NVDA",
        limit=10,
        include_conversations=False,
        workspace_id=ws_uuid,
    )

    assert len(captured_file_queries) == 1
    sql, params = captured_file_queries[0]
    assert "w.user_id = %s" in sql
    assert "f.workspace_id = %s::uuid" in sql
    assert sql.index("f.workspace_id = %s::uuid") < sql.index("LIMIT %s")
    assert params is not None
    assert params[0] == "user-alice"
    assert params[1] == ws_uuid
    assert params[-1] == 10

    assert [(d["kind"], d["workspace_id"], d["path"]) for d in res["documents"]] == [
        ("memo", None, ".agents/user/memo/personal.md"),
        ("workspace_file", ws_uuid, "notes/target.md"),
    ]


@pytest.mark.asyncio
async def test_include_content_false_skips_content_text_sql_and_sets_5s_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When include_content=False, workspace_files SQL omits content_text ILIKE and uses 5s timeout."""
    ws_uuid = "11111111-1111-4111-8111-111111111111"

    def _responder(sql: str, _params: tuple[Any, ...] | None) -> list[dict[str, Any]]:
        if "FROM workspace_files f" in sql:
            assert "content_text ILIKE" not in sql
            assert "NULL::text AS content_text" in sql
            return [
                {
                    "file_id": "f-name",
                    "workspace_id": ws_uuid,
                    "user_id": "user-alice",
                    "status": "running",
                    "workspace_name": "Semis",
                    "file_path": "notes/nvda_name.md",
                    "file_name": "nvda_name.md",
                    "content_text": None,
                    "sandbox_modified_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                }
            ]
        return []

    fake_get_conn, executed = _make_fake_db(_responder)
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search(
        "user-alice",
        "nvda",
        limit=10,
        include_conversations=False,
        include_content=False,
    )

    assert [d["path"] for d in res["documents"]] == ["notes/nvda_name.md"]
    assert res.get("content_timed_out") is None
    assert not any(sql.strip() == CONTENT_STATEMENT_TIMEOUT_SQL for sql, _ in executed)


@pytest.mark.asyncio
async def test_content_search_timeout_degrades_to_name_hits_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If 15s content search raises QueryCanceled, falls back to name/path matches and marks content_timed_out."""
    ws_uuid = "11111111-1111-4111-8111-111111111111"
    call_log: list[bool] = []

    async def _fake_search_ws(
        _db_conn: Any,
        _user_id: str,
        _like: str,
        _limit: int,
        *,
        norm_workspace_id: str | None,
        include_content: bool,
    ) -> list[dict[str, Any]]:
        call_log.append(include_content)
        if include_content:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return [
            {
                "file_id": "f-fallback",
                "workspace_id": ws_uuid,
                "user_id": "user-alice",
                "status": "running",
                "workspace_name": "Semis",
                "file_path": "notes/margin_fallback.md",
                "file_name": "margin_fallback.md",
                "content_text": None,
                "sandbox_modified_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc),
            }
        ]

    fake_ws_helper = create_autospec(search_db._search_workspace_files_rows, side_effect=_fake_search_ws)
    monkeypatch.setattr(search_db, "_search_workspace_files_rows", fake_ws_helper)
    fake_get_conn, _ = _make_fake_db(lambda _sql, _params: [])
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search(
        "user-alice",
        "margin",
        limit=10,
        include_conversations=False,
        include_content=True,
    )

    assert call_log == [True, False]
    assert res["content_timed_out"] is True
    assert [d["path"] for d in res["documents"]] == ["notes/margin_fallback.md"]


@pytest.mark.asyncio
async def test_invalid_workspace_id_returns_422_and_skips_file_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid workspace_id UUID returns 422 at the router and skips workspace_files SQL in DB helpers."""
    fake_search = create_autospec(
        search_db.global_search,
        return_value={"conversations": [], "documents": []},
    )
    monkeypatch.setattr(search_db, "global_search", fake_search)

    app = FastAPI()
    app.include_router(dalton_router)
    app.dependency_overrides[get_current_user_id] = lambda: "user-alice"
    monkeypatch.setattr("src.dalton.app.router._require_dalton_enabled", lambda: None)

    client = TestClient(app)
    resp = client.get("/api/v1/dalton/search", params={"q": "memo", "workspace_id": "not-a-uuid"})
    assert resp.status_code == 422
    fake_search.assert_not_awaited()

    monkeypatch.undo()
    fake_get_conn, executed = _make_fake_db(lambda _sql, _params: [])
    monkeypatch.setattr(search_db, "get_db_connection", fake_get_conn)

    res = await global_search(
        "user-alice",
        "memo",
        limit=10,
        include_conversations=False,
        workspace_id="not-a-uuid",
    )
    assert res["documents"] == []
    assert not any("FROM workspace_files" in sql for sql, _ in executed)

